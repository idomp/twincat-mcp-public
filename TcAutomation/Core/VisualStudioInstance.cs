using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.IO;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using Microsoft.Win32;
using System.Threading;
using EnvDTE80;
using TCatSysManagerLib;

namespace TcAutomation.Core
{
    /// <summary>
    /// Manages a Visual Studio DTE instance for TwinCAT automation.
    /// 
    /// Handles:
    /// - Creating/loading VS DTE (TcXaeShell or Visual Studio)
    /// - Opening TwinCAT solutions
    /// - Building solutions
    /// - Extracting errors from Error List
    /// </summary>
    public class VisualStudioInstance : IDisposable
    {
        private string _solutionFilePath;
        private string _tcVersion;
        private string? _forceTcVersion;
        
        private DTE2? _dte;
        private EnvDTE.Solution? _solution;
        private EnvDTE.Project? _tcProject;
        private bool _loaded;

        // The shell this instance started. The Process object keeps the handle
        // CreateProcess returned, so HasExited, StartTime and Kill act on that
        // process and never on whatever later reuses its PID. Nothing else is
        // ever terminated: an IDE the user opens is not ours, whenever it starts.
        private Process? _ownedProcess;
        private readonly object _ownedProcessLock = new object();
        // Set by Retire(). Checked under _ownedProcessLock before Process.Start,
        // so a shutdown that retires this instance cannot race a launch.
        private bool _launchClosed;

        // Silent-reload state. When we change DTE options (AutoloadExternalChanges,
        // etc.) we remember the previous value so we can restore it on Close()
        // — DTE writes these options back to the user's registry profile, so
        // leaving them tweaked would leak into the user's interactive TcXaeShell.
        private readonly List<(string Category, string Page, string Item, object? Original)> _savedPreferences
            = new List<(string, string, string, object?)>();

        // Belt-and-suspenders visibility watchdog. If any modal we couldn't
        // suppress promotes the main window (modals need a visible parent),
        // this thread flips MainWindow.Visible back to false within ~500ms so
        // the user doesn't see a TcXaeShell frame flash into view.
        private Thread? _visibilityWatchdog;
        private volatile bool _watchdogShouldStop;

        // Tracks whether we've registered this DTE's PID with the static
        // DialogWatchdog. We need to know so Close() can Stop the exact
        // PID we Start'd (and not blow away a Stop call that already ran
        // from some other teardown path).
        private int? _dialogWatchdogPid;

        /// <summary>
        /// The Windows PID of the TcXaeShell/devenv process we launched.
        /// Taken from Process.Start, before attaching to that exact PID in the
        /// Running Object Table. Used by the persistent host to write session
        /// files. Termination goes through <see cref="KillOwnedProcess"/>,
        /// never through this number.
        /// </summary>
        public int? DteProcessId { get; private set; }

        /// <summary>
        /// Start-time fingerprint of the owned shell, in the round-trip format
        /// SessionFile compares against. Read from the launch handle, so it
        /// cannot describe a different process that reused the PID.
        /// </summary>
        public string? DteProcessStartTimeUtc { get; private set; }

        /// <summary>
        /// Called once the owned shell exists, before the up-to-two-minute wait
        /// for its DTE. Lets the host record the PID while startup can still
        /// fail or the worker can still be killed.
        /// </summary>
        public Action<VisualStudioInstance>? OwnedProcessStarted { get; set; }

        /// <summary>
        /// True once a termination attempt could not confirm that the owned
        /// shell exited. The host must then keep its session record, so the
        /// janitor can still find the shell by PID and fingerprint.
        /// </summary>
        public bool OwnedProcessLeftRunning { get; private set; }

        /// <summary>
        /// True once a solution is successfully opened and a TwinCAT project is found.
        /// Cleared by ReloadSolution() until the new solution is fully loaded.
        /// </summary>
        public bool IsSolutionLoaded => _loaded;

        /// <summary>
        /// The solution path currently loaded (or most recently requested).
        /// </summary>
        public string SolutionFilePath => _solutionFilePath;

        public VisualStudioInstance(string solutionFilePath, string tcVersion, string? forceTcVersion = null)
        {
            _solutionFilePath = solutionFilePath;
            _tcVersion = tcVersion;
            // PowerShell's string binding can turn a supplied $null into "".
            // An absent override must not hide the required project baseline.
            _forceTcVersion = string.IsNullOrWhiteSpace(forceTcVersion) ? null : forceTcVersion;
        }

        /// <summary>
        /// Load the Visual Studio DTE instance.
        /// </summary>
        public void Load()
        {
            // Determine VS version from solution
            var vsVersion = TcFileUtilities.GetVisualStudioVersion(_solutionFilePath) ?? "17.0";
            
            LoadDevelopmentToolsEnvironment(vsVersion);
        }

        /// <summary>
        /// Open the solution and find the TwinCAT project.
        /// </summary>
        public void LoadSolution()
        {
            if (_dte == null)
                throw new InvalidOperationException("DTE not loaded. Call Load() first.");

            // Delete the .suo file before opening. The .suo stores per-user
            // window state (open documents, docking layout, bookmarks). If it
            // has open documents, VS auto-reopens them when the solution loads
            // — which causes "File changed outside environment" dialogs to
            // spam throughout build/activate as TwinCAT rewrites those files.
            // Deleting it has no effect on actual project data.
            try
            {
                string suoDir = System.IO.Path.Combine(
                    System.IO.Path.GetDirectoryName(_solutionFilePath) ?? "",
                    ".vs",
                    System.IO.Path.GetFileNameWithoutExtension(_solutionFilePath));
                if (System.IO.Directory.Exists(suoDir))
                {
                    foreach (var suo in System.IO.Directory.GetFiles(suoDir, "*.suo", System.IO.SearchOption.AllDirectories))
                    {
                        try { System.IO.File.Delete(suo); Console.Error.WriteLine($"[DEBUG] Deleted stale .suo: {suo}"); }
                        catch { }
                    }
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[DEBUG] .suo cleanup failed (non-fatal): {ex.Message}");
            }

            _solution = _dte.Solution;
            _solution.Open(_solutionFilePath);

            // Wait for solution to load and find TwinCAT project
            // TwinCAT projects can take a while to fully load
            for (int attempt = 1; attempt <= 30; attempt++)
            {
                Thread.Sleep(1000);

                try
                {
                    for (int i = 1; i <= _solution.Projects.Count; i++)
                    {
                        EnvDTE.Project? proj;
                        try { proj = _solution.Projects.Item(i); }
                        catch { continue; }

                        // Check if this project has ITcSysManager (TwinCAT project)
                        try
                        {
                            if (proj.Object is ITcSysManager)
                            {
                                _tcProject = proj;
                                _loaded = true;
                                // Re-hide after solution open: the shell will
                                // often restore visibility while loading projects.
                                HideMainWindow();
                                return;
                            }
                        }
                        catch { }
                    }
                }
                catch { }
            }

            throw new InvalidOperationException("No TwinCAT project found in solution after 30 seconds.");
        }

        /// <summary>
        /// Close the currently-loaded solution (if any) and open a different one
        /// in the SAME DTE instance. Avoids paying the ~25-30s shell-startup cost
        /// when the host is asked to switch projects. Only the solution-close /
        /// solution-open cost applies (seconds).
        /// </summary>
        public void ReloadSolution(string newSolutionFilePath, string newTcVersion, string? newForceTcVersion = null)
        {
            if (_dte == null)
                throw new InvalidOperationException("DTE not loaded. Call Load() first.");

            // Close the existing solution without saving. We intentionally do not
            // call DTE.Solution.Close(true) because TwinCAT can rewrite files on
            // close, which triggers modal save dialogs.
            try
            {
                if (_solution != null)
                {
                    try
                    {
                        if (_solution.IsOpen)
                        {
                            _solution.Close(false);
                            Thread.Sleep(500);
                        }
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine($"[DEBUG] Solution.Close raised (non-fatal): {ex.Message}");
                    }
                }
            }
            finally
            {
                _solution = null;
                _tcProject = null;
                _loaded = false;
            }

            _solutionFilePath = newSolutionFilePath;
            _tcVersion = newTcVersion;
            _forceTcVersion = newForceTcVersion;

            // Switch TC version in-place. Different solutions may target different
            // versions; this is cheap when we're just changing a registry-backed
            // selector on the remote manager.
            LoadTwinCATVersion();

            LoadSolution();
        }

        /// <summary>
        /// Close all open documents in the DTE without saving. Prevents the
        /// "File has been changed outside the environment" and "Conflicting
        /// File Modification Detected" modal dialogs from appearing when
        /// TwinCAT rewrites files (like PlcTask.TcTTO) during build/activate.
        /// The .suo file from prior IDE sessions can cause documents to be
        /// reopened automatically when our headless DTE loads the solution,
        /// so this must be called right after LoadSolution().
        /// </summary>
        public void CloseAllDocuments()
        {
            if (_dte == null) return;

            try
            {
                var docs = new System.Collections.Generic.List<EnvDTE.Document>();
                foreach (EnvDTE.Document d in _dte.Documents)
                    docs.Add(d);

                foreach (var doc in docs)
                {
                    try { doc.Close(EnvDTE.vsSaveChanges.vsSaveChangesNo); }
                    catch { }
                }

                // Also close any open windows (tool windows excluded by filter).
                var windows = new System.Collections.Generic.List<EnvDTE.Window>();
                foreach (EnvDTE.Window w in _dte.Windows)
                {
                    if (w.Kind == "Document") windows.Add(w);
                }
                foreach (var w in windows)
                {
                    try { w.Close(EnvDTE.vsSaveChanges.vsSaveChangesNo); }
                    catch { }
                }

                Console.Error.WriteLine($"[DEBUG] Closed {docs.Count} document(s) and {windows.Count} window(s) in DTE");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[DEBUG] CloseAllDocuments failed: {ex.Message}");
            }
        }

        /// <summary>
        /// Get the TwinCAT project.
        /// </summary>
        public EnvDTE.Project GetProject()
        {
            if (_tcProject == null)
                throw new InvalidOperationException("Project not loaded. Call LoadSolution() first.");
            return _tcProject;
        }

        /// <summary>
        /// Get the TwinCAT System Manager interface.
        /// </summary>
        public ITcSysManager10 GetSystemManager()
        {
            if (_tcProject?.Object == null)
                throw new InvalidOperationException("Project not loaded.");
            return (ITcSysManager10)_tcProject.Object;
        }

        /// <summary>
        /// Clean the solution.
        /// </summary>
        public void CleanSolution()
        {
            if (_solution == null)
                throw new InvalidOperationException("Solution not loaded.");

            _solution.SolutionBuild.Clean(true);
            Thread.Sleep(2000);
        }

        /// <summary>
        /// Build the solution. Uses async Build(false) + SpinWait on BuildState
        /// to reliably wait until the build completes, matching the pattern used
        /// by TcUnit-Runner. Synchronous Build(true) + Sleep is unreliable.
        /// </summary>
        public void BuildSolution()
        {
            if (_solution == null)
                throw new InvalidOperationException("Solution not loaded.");

            _solution.SolutionBuild.Build(false);
            System.Threading.SpinWait.SpinUntil(
                () => _solution.SolutionBuild.BuildState == EnvDTE.vsBuildState.vsBuildStateDone);
        }

        /// <summary>
        /// Get error items from the Error List window.
        /// </summary>
        public ErrorItems GetErrorItems()
        {
            if (_dte == null)
                throw new InvalidOperationException("DTE not loaded.");
            return _dte.ToolWindows.ErrorList.ErrorItems;
        }

        public DTE2 Dte => _dte ?? throw new InvalidOperationException("DTE not loaded.");

        public int LastBuildInfo => _solution.SolutionBuild.LastBuildInfo;

        public string GetBuildOutput()
        {
            var output = new System.Text.StringBuilder();
            foreach (EnvDTE.OutputWindowPane pane in _dte.ToolWindows.OutputWindow.OutputWindowPanes)
            {
                try
                {
                    var doc = pane.TextDocument;
                    output.AppendLine("[" + pane.Name + "]");
                    output.AppendLine(doc.StartPoint.CreateEditPoint().GetText(doc.EndPoint));
                }
                catch (System.Runtime.InteropServices.COMException) { }
            }
            return output.ToString();
        }

        /// <summary>
        /// Close Visual Studio instance. Terminates the owned shell through its
        /// launch handle if DTE.Quit() leaves it running.
        /// </summary>
        public void Close()
        {
            // Stop the watchdog FIRST so it doesn't race DTE teardown by
            // calling MainWindow on a dying COM object.
            StopVisibilityWatchdog();

            // Deregister from the dialog-dismisser before the PID becomes
            // reused by the OS. Stop is idempotent / ref-counted, so
            // double-Stop (e.g. from Dispose) is safe.
            if (_dialogWatchdogPid.HasValue)
            {
                try { DialogWatchdog.Stop(_dialogWatchdogPid.Value); } catch { }
                _dialogWatchdogPid = null;
            }

            try
            {
                if (_dte != null)
                {
                    // Restore any user preferences we tweaked at startup BEFORE
                    // Quit, because Quit may persist the current (our-modified)
                    // values to the registry profile.
                    RestoreDteOptions();

                    Thread.Sleep(3000); // Avoid busy errors
                    try
                    {
                        _dte.Quit();
                        Thread.Sleep(5000);
                    }
                    catch { }
                }
            }
            finally
            {
                // Runs whether or not a DTE was ever attached. Startup can fail
                // after the shell started but before it published its DTE.
                KillOwnedProcess("close");
                _dte = null;
                _solution = null;
                _tcProject = null;
                _loaded = false;
            }
        }

        /// <summary>
        /// Terminate the shell this instance started, through the launch
        /// handle. Touches no other process. Safe from any thread and safe to
        /// repeat.
        ///
        /// The whole attempt runs under one lock, so a second caller waits for
        /// the first attempt's result instead of reading "nothing to do" while
        /// the first is still terminating. Returns true once the shell is
        /// confirmed gone, or when none was started. On false the handle is
        /// KEPT, so a later call can retry, and the PID stays recorded.
        /// </summary>
        public bool KillOwnedProcess(string reason)
        {
            lock (_ownedProcessLock)
            {
                var owned = _ownedProcess;
                if (owned == null) return true;

                bool exited;
                try
                {
                    if (!owned.HasExited)
                    {
                        Console.Error.WriteLine($"[DEBUG] Terminating owned DTE process (PID {owned.Id}, {reason})");
                        owned.Kill();
                    }
                    exited = owned.WaitForExit(5000);
                }
                catch (Exception ex)
                {
                    // Kill throws when the process exited between the checks. The
                    // handle answers that question without guessing.
                    try { exited = owned.HasExited; } catch { exited = false; }
                    if (!exited)
                        Console.Error.WriteLine($"[DEBUG] Owned DTE cleanup failed: {ex.Message}");
                }

                if (!exited)
                {
                    Console.Error.WriteLine($"[DEBUG] Owned DTE process PID {owned.Id} is not confirmed gone");
                    OwnedProcessLeftRunning = true;
                    return false;
                }

                _ownedProcess = null;
                owned.Dispose();
                OwnedProcessLeftRunning = false;
                DteProcessId = null;
                DteProcessStartTimeUtc = null;
                return true;
            }
        }

        /// <summary>
        /// Refuse any further launch by this instance, then terminate its
        /// shell. Used by shutdown, which can run while a launch is in
        /// progress on the STA thread.
        /// </summary>
        public bool Retire(string reason)
        {
            lock (_ownedProcessLock) { _launchClosed = true; }
            return KillOwnedProcess(reason);
        }

        public void Dispose()
        {
            Close();
        }

        private void LoadDevelopmentToolsEnvironment(string vsVersion)
        {
            // COM activation launches through the service host, whose environment
            // can predate TwinCAT installation. Launch our own process with the
            // native-library search path, then attach only to that exact PID.
            string[] progIds = new[]
            {
                $"TcXaeShell.DTE.{vsVersion}",
                $"VisualStudio.DTE.{vsVersion}",
                "TcXaeShell.DTE.17.0",
                "TcXaeShell.DTE.15.0",
                "VisualStudio.DTE.17.0",
            };
            Exception? lastError = null;
            // One gate wait for the whole loop. A wait per ProgID would add up
            // past the client's ensure-solution budget.
            using (AcquireLaunchGate())
            foreach (var progId in progIds.Distinct())
            {
                var command = FindLocalServerCommand(progId);
                if (command == null) continue;
                try
                {
                    LaunchAndAttach(progId, command);
                    break;
                }
                catch (OperationCanceledException)
                {
                    // Shutdown retired this instance. Start nothing else.
                    _dte = null;
                    KillOwnedProcess("launch cancelled");
                    throw;
                }
                catch (Exception ex)
                {
                    lastError = ex;
                    Console.Error.WriteLine($"[DEBUG] DTE launch {progId} failed: {ex}");
                    _dte = null;
                    bool started = DteProcessId != null;
                    // A shell that will not die keeps its handle and its PID
                    // record. Starting another would replace that record and
                    // hide the first shell from the janitor.
                    if (!KillOwnedProcess("launch failed"))
                        throw new InvalidOperationException(
                            $"XAE PID {DteProcessId?.ToString() ?? "unknown"} did not exit after a failed launch. " +
                            "No other shell is started.", ex);
                    // Only a registration that cannot start at all moves on to
                    // the next ProgID. A shell that started and then died, never
                    // published its DTE, or lost its activation claim is a
                    // failure to report, not a reason to start a different IDE
                    // while the launch gate is held.
                    if (started)
                        throw new InvalidOperationException(
                            $"{progId} started but did not become usable: {ex.Message}", ex);
                }
            }

            if (_dte == null)
                throw new InvalidOperationException(
                    "Could not load TcXaeShell or Visual Studio DTE. Ensure TwinCAT XAE is installed." +
                    (lastError != null ? " Last error: " + lastError.Message : ""), lastError);

            // Outside the ProgID loop and the launch gate. A version the shell
            // cannot provide is a refusal, not a reason to start a different IDE.
            try
            {
                ConfigureDte();
                LoadTwinCATVersion();
            }
            catch
            {
                Close();
                throw;
            }
        }

        /// <summary>
        /// Serializes start, ROT attach and activation claim across every
        /// worker in this logon session. Two workers starting at once could
        /// otherwise each claim the other's registration, leaving one
        /// registration open to a third client, and releasing the foreign
        /// claim can shut down a shell its own worker has not attached yet.
        /// </summary>
        private const string LaunchGateName = @"Local\twincat-mcp-xae-launch";

        /// <summary>
        /// How long a worker waits for another worker's launch. A holder
        /// keeps the gate for its start, its ROT attach (bounded at 120 s),
        /// its claim, and after a failed start its teardown. The claim is not
        /// bounded: it returns at once normally, but when another program took
        /// the registration, COM starts a new shell for it first (20 to 35 s
        /// measured). A waiter can then time out; it starts nothing and
        /// reports that.
        /// mcp-server/twincat_mcp/host.py sizes the client's ensure-solution
        /// budget (ENSURE_SOLUTION_TIMEOUT_SEC) from this value.
        /// </summary>
        private const int LaunchGateWaitSeconds = 150;

        private static IDisposable AcquireLaunchGate()
        {
            Mutex gate;
            try
            {
                gate = new Mutex(false, LaunchGateName);
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException || ex is WaitHandleCannotBeOpenedException)
            {
                // Typically a worker at another integrity level holds it. Say
                // so, instead of letting it read as a missing installation.
                throw new InvalidOperationException(
                    $"The shell launch gate '{LaunchGateName}' cannot be opened ({ex.Message}). " +
                    "Another worker, possibly elevated, is starting a shell. No shell is started.", ex);
            }

            bool held;
            try { held = gate.WaitOne(TimeSpan.FromSeconds(LaunchGateWaitSeconds)); }
            catch (AbandonedMutexException) { held = true; }   // the previous holder died
            if (!held)
            {
                gate.Dispose();
                throw new TimeoutException(
                    $"Another worker's shell launch did not finish within {LaunchGateWaitSeconds} s. No shell is started.");
            }
            return new GateRelease(gate);
        }

        /// <summary>Releases the gate on the thread that acquired it.</summary>
        private sealed class GateRelease : IDisposable
        {
            private Mutex? _gate;
            public GateRelease(Mutex gate) { _gate = gate; }
            public void Dispose()
            {
                var gate = _gate;
                _gate = null;
                if (gate == null) return;
                try { gate.ReleaseMutex(); }
                finally { gate.Dispose(); }
            }
        }

        private void LaunchAndAttach(string progId, string registeredCommand)
        {
            var start = CreateDevelopmentToolsStartInfo(registeredCommand);
            Process process;
            lock (_ownedProcessLock)
            {
                // Under the same lock as Retire(). Once shutdown has retired
                // this instance, no shell can start behind its back.
                if (_launchClosed)
                    throw new OperationCanceledException("The host is shutting down. No shell is started.");
                if (_ownedProcess != null)
                    throw new InvalidOperationException(
                        $"This instance still owns XAE PID {_ownedProcess.Id}. No second shell is started.");
                process = Process.Start(start)
                    ?? throw new InvalidOperationException("XAE process did not start.");
                _ownedProcess = process;
                OwnedProcessLeftRunning = false;
            }
            DteProcessId = process.Id;
            try { DteProcessStartTimeUtc = process.StartTime.ToUniversalTime().ToString("O"); }
            catch { DteProcessStartTimeUtc = null; }
            Console.Error.WriteLine($"[DEBUG] Launched owned DTE process PID: {DteProcessId}");

            try { OwnedProcessStarted?.Invoke(this); }
            catch (Exception ex) { Console.Error.WriteLine($"[DEBUG] OwnedProcessStarted handler failed: {ex.Message}"); }

            var timer = Stopwatch.StartNew();
            while (timer.Elapsed < TimeSpan.FromSeconds(120))
            {
                if (process.HasExited)
                    throw new InvalidOperationException($"XAE exited before publishing DTE (exit {process.ExitCode}).");
                _dte = FindOwnedDte(progId, process.Id);
                if (_dte != null)
                {
                    ClaimOwnActivation(progId, process.Id);
                    return;
                }
                Thread.Sleep(250);
            }
            throw new TimeoutException($"XAE PID {process.Id} did not publish its DTE within 120 seconds.");
        }

        /// <summary>
        /// A shell started with -Embedding registers its DTE class object for
        /// exactly one activation (measured: the first CoCreateInstance from
        /// another process received this shell, the second started a new
        /// one). Left unclaimed, the next activation of the ProgID by ANY
        /// process on the machine is served by this shell, and that client
        /// can open solutions in it or quit it. Claim it here, once, right
        /// after attaching. Measured: the claim returns this same shell, no
        /// extra shell starts, and the shell survives the release.
        ///
        /// A claim that returns anything else means another program can hold
        /// this shell, or can still take it. That is an ownership failure:
        /// the caller terminates this shell and reports the launch failed.
        /// </summary>
        private void ClaimOwnActivation(string progId, int processId)
        {
            object? claimed = null;
            bool own = false;
            try
            {
                var type = Type.GetTypeFromProgID(progId)
                    ?? throw new InvalidOperationException($"{progId} is no longer registered.");
                claimed = Activator.CreateInstance(type);
                own = claimed != null && _dte != null && SameComIdentity(claimed, _dte);
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException(
                    $"Could not claim the {progId} activation registered by PID {processId}: {ex.Message}. " +
                    "Another program could attach to this shell, so it is not used.", ex);
            }
            finally
            {
                // The same object as _dte shares its runtime wrapper, so
                // releasing it would release the attached DTE too.
                //
                // Another shell's object is only released, never quit or
                // killed. If the claim found no pending registration, COM
                // started a new shell for it, and that shell exits on its own
                // once this, its only reference, is released (measured: 24 s,
                // both at once and after 10 s idle). A shell another program
                // started keeps that program's references and stays up.
                if (claimed != null && !own && Marshal.IsComObject(claimed))
                    Marshal.ReleaseComObject(claimed);
            }
            if (!own)
                throw new InvalidOperationException(
                    $"The {progId} activation claim for PID {processId} was served by another shell. " +
                    "Another program could hold this shell's registration, so it is not used.");
            Console.Error.WriteLine($"[DEBUG] Claimed the activation registered by owned PID {processId}");
        }

        private static bool SameComIdentity(object a, object b)
        {
            if (ReferenceEquals(a, b)) return true;
            IntPtr pa = IntPtr.Zero, pb = IntPtr.Zero;
            try
            {
                pa = Marshal.GetIUnknownForObject(a);
                pb = Marshal.GetIUnknownForObject(b);
                return pa == pb;
            }
            finally
            {
                if (pa != IntPtr.Zero) Marshal.Release(pa);
                if (pb != IntPtr.Zero) Marshal.Release(pb);
            }
        }

        /// <summary>
        /// The LocalServer32 command registered for a ProgID. Checks the 64-bit
        /// registry view, then the 32-bit one. This worker is x64, and
        /// TcXaeShell 15.0 registers its DTE class only in the 32-bit view, so
        /// reading the default view alone finds no shell at all.
        /// </summary>
        private static string? FindLocalServerCommand(string progId)
        {
            Type? type;
            try { type = Type.GetTypeFromProgID(progId); }
            catch { return null; }
            if (type == null) return null;

            foreach (var view in new[] { RegistryView.Registry64, RegistryView.Registry32 })
            {
                try
                {
                    using (var root = RegistryKey.OpenBaseKey(RegistryHive.ClassesRoot, view))
                    using (var key = root.OpenSubKey($@"CLSID\{{{type.GUID}}}\LocalServer32"))
                    {
                        var command = key?.GetValue(null) as string;
                        if (!string.IsNullOrWhiteSpace(command)) return command;
                    }
                }
                catch { }
            }
            return null;
        }

        private static ProcessStartInfo CreateDevelopmentToolsStartInfo(string registeredCommand)
        {
            var command = Environment.ExpandEnvironmentVariables(registeredCommand.Trim());
            string executable;
            string arguments;
            if (command.StartsWith("\"", StringComparison.Ordinal))
            {
                var end = command.IndexOf('"', 1);
                if (end < 0) throw new InvalidOperationException("Unterminated registered XAE executable path.");
                executable = command.Substring(1, end - 1);
                arguments = command.Substring(end + 1).Trim();
            }
            else
            {
                // LocalServer32 commonly stores an unquoted path containing spaces.
                var end = command.IndexOf(".exe", StringComparison.OrdinalIgnoreCase);
                if (end < 0) throw new InvalidOperationException("Registered XAE server is not an executable.");
                executable = command.Substring(0, end + 4);
                arguments = command.Substring(end + 4).Trim();
            }
            if (!Path.IsPathRooted(executable) || !File.Exists(executable))
                throw new FileNotFoundException("Registered XAE executable does not exist.", executable);
            // Preserve the automation-server startup mode formerly supplied by
            // COM activation; a normal interactive launch has different ownership.
            if (!arguments.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries)
                .Any(arg => string.Equals(arg, "-Embedding", StringComparison.OrdinalIgnoreCase) ||
                    string.Equals(arg, "/Embedding", StringComparison.OrdinalIgnoreCase)))
                arguments = (arguments + " -Embedding").Trim();
            var start = new ProcessStartInfo(executable, arguments)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden,
                WorkingDirectory = Path.GetDirectoryName(executable)!,
            };
            var root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Beckhoff", "TwinCAT");
            // Legacy shells can be 32-bit even though this automation host is x64.
            // Put the launched executable's native directory first.
            bool x86;
            using (var reader = new BinaryReader(File.OpenRead(executable)))
            {
                reader.BaseStream.Position = 0x3c;
                var peOffset = reader.ReadInt32();
                reader.BaseStream.Position = peOffset;
                if (reader.ReadUInt32() != 0x00004550) throw new InvalidOperationException("Invalid XAE PE signature.");
                x86 = reader.ReadUInt16() == 0x014c;
            }
            var common = new[] { Path.Combine(root, x86 ? "Common32" : "Common64"), Path.Combine(root, x86 ? "Common64" : "Common32") };
            start.EnvironmentVariables["PATH"] = string.Join(";", common.Where(Directory.Exists)
                .Concat(new[] { start.EnvironmentVariables["PATH"] ?? string.Empty }));
            return start;
        }

        [DllImport("ole32.dll")]
        private static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable table);
        [DllImport("ole32.dll")]
        private static extern int CreateBindCtx(int reserved, out IBindCtx context);

        private static DTE2? FindOwnedDte(string progId, int processId)
        {
            IRunningObjectTable? table = null;
            IBindCtx? context = null;
            IEnumMoniker? enumerator = null;
            try
            {
                Marshal.ThrowExceptionForHR(GetRunningObjectTable(0, out table));
                Marshal.ThrowExceptionForHR(CreateBindCtx(0, out context));
                table.EnumRunning(out enumerator);
                var monikers = new IMoniker[1];
                while (enumerator.Next(1, monikers, IntPtr.Zero) == 0)
                {
                    try
                    {
                        monikers[0].GetDisplayName(context, null, out var name);
                        if (!IsOwnedDteMoniker(name, progId, processId)) continue;
                        table.GetObject(monikers[0], out var value);
                        if (value is DTE2 dte) return dte;
                        if (Marshal.IsComObject(value)) Marshal.ReleaseComObject(value);
                    }
                    catch (COMException) { /* A startup/closing ROT entry can be temporarily unavailable. */ }
                    catch (UnauthorizedAccessException) { /* E_ACCESSDENIED maps to this managed type; skip protected/unavailable ROT entries. */ }
                    finally { Marshal.ReleaseComObject(monikers[0]); }
                }
            }
            catch (COMException) { /* Retry until the owned shell has finished startup. */ }
            catch (UnauthorizedAccessException) { /* Retry bounded attachment; never change permissions or select another PID. */ }
            finally
            {
                if (enumerator != null) Marshal.ReleaseComObject(enumerator);
                if (context != null) Marshal.ReleaseComObject(context);
                if (table != null) Marshal.ReleaseComObject(table);
            }
            return null;
        }

        private static bool IsOwnedDteMoniker(string name, string progId, int processId)
        {
            // XAE shells may publish the VisualStudio alias. Never attach merely
            // by ProgID: interactive and automated sessions can coexist.
            var suffix = ":" + processId.ToString(System.Globalization.CultureInfo.InvariantCulture);
            var plain = name.TrimStart('!');
            var alias = progId.StartsWith("TcXaeShell.", StringComparison.OrdinalIgnoreCase)
                ? "VisualStudio." + progId.Substring("TcXaeShell.".Length)
                : "TcXaeShell." + progId.Substring("VisualStudio.".Length);
            return string.Equals(plain, progId + suffix, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(plain, alias + suffix, StringComparison.OrdinalIgnoreCase);
        }

        private void ConfigureDte()
        {
            if (_dte == null) return;

            _dte.UserControl = false;
            _dte.SuppressUI = true;

            // Hide the main window so the shell runs truly headless.
            // This is the reason users would see a TcXaeShell window pop up
            // during every build/activate/deploy — SuppressUI only hides
            // modal dialogs, not the IDE frame itself.
            //
            // MainWindow can be briefly unavailable immediately after the
            // process launch and ROT attachment (the shell is still initializing
            // its UI thread), so retry a few times before giving up.
            HideMainWindow();

            // Configure error list to capture all types
            _dte.ToolWindows.ErrorList.ShowErrors = true;
            _dte.ToolWindows.ErrorList.ShowMessages = true;
            _dte.ToolWindows.ErrorList.ShowWarnings = true;

            // Silent-reload: when an external edit touches a file that VS is
            // tracking, reload it silently instead of popping the
            // "This item has been modified outside of the source editor.
            //  Do you want to reload it?" modal. That modal is the reason
            // users occasionally see our hidden main window flash into view —
            // a modal dialog needs a visible parent window, so VS promotes
            // the main window out from under us to attach it.
            //
            // We KEEP DetectFileChangesOutsideIDE=true so the shell still
            // picks up edits (otherwise a subsequent build would compile
            // stale content); we just don't want the dialog.
            //
            // AutoloadExternalChanges only fires silently when there are no
            // in-memory edits to the file. Our headless shell never opens
            // documents interactively (we even delete the .suo to keep it
            // that way), so the precondition is always met.
            ApplyDteOption("Environment", "Documents", "DetectFileChangesOutsideIDE", true);
            ApplyDteOption("Environment", "Documents", "AutoloadExternalChanges", true);

            // Enable TwinCAT silent mode
            try
            {
                var settings = (ITcAutomationSettings)_dte.GetObject("TcAutomationSettings");
                settings.SilentMode = true;
                Console.Error.WriteLine("[DEBUG] TcAutomationSettings.SilentMode set to true");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"[DEBUG] Failed to set SilentMode: {ex.Message}");
            }

            // Start the visibility watchdog last, after all configuration is
            // done. If anything above somehow unhid the window (race with UI
            // thread), the watchdog will re-hide on its first tick.
            StartVisibilityWatchdog();

            // Start the dialog-dismisser, scoped to OUR spawned DTE PID so
            // it cannot touch dialogs in the user's own IDE or any other
            // shell on the box. This is what silences the project-reload
            // prompt the user sees when they (or another AI) edit TwinCAT
            // project files while our headless shell is alive.
            //
            // AutoloadExternalChanges (set above) handles DOCUMENT reloads
            // silently, but project-level reload is a separate modal with
            // no DTE setting that disables it — the only reliable silencer
            // is to identify the dialog by its body text and click Reload
            // programmatically. The watchdog does exactly that.
            if (DteProcessId.HasValue && DteProcessId.Value > 0)
            {
                DialogWatchdog.Start(DteProcessId.Value);
                _dialogWatchdogPid = DteProcessId.Value;
                Console.Error.WriteLine(
                    $"[DEBUG] DialogWatchdog registered for DTE PID {DteProcessId.Value}");
            }
            else
            {
                Console.Error.WriteLine(
                    "[DEBUG] DialogWatchdog NOT started: DteProcessId unknown. " +
                    "Modal-reload dialogs will not be auto-dismissed for this session.");
            }
        }

        /// <summary>
        /// Apply a DTE option and remember the previous value so we can
        /// restore it on Close(). DTE options can persist to the user's
        /// registry profile, so restoring is important to avoid leaking our
        /// session-local tweaks into the user's interactive shell.
        ///
        /// Silently swallows failures — the property may not exist on every
        /// shell SKU / version, and a missing preference isn't worth
        /// aborting startup over.
        /// </summary>
        private void ApplyDteOption(string category, string page, string item, object newValue)
        {
            if (_dte == null) return;

            try
            {
                var props = _dte.Properties[category, page];
                var prop = props.Item(item);
                object? original = null;
                try { original = prop.Value; } catch { /* readable-only in some SKUs */ }

                _savedPreferences.Add((category, page, item, original));
                prop.Value = newValue;
                Console.Error.WriteLine(
                    $"[DEBUG] DTE option {category}.{page}.{item}: {original ?? "(unknown)"} -> {newValue}");
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(
                    $"[DEBUG] DTE option {category}.{page}.{item} not applied (non-fatal): {ex.Message}");
            }
        }

        /// <summary>
        /// Restore every DTE option we tweaked in ConfigureDte. Best-effort:
        /// if the DTE is already tearing down, we swallow errors.
        /// </summary>
        private void RestoreDteOptions()
        {
            if (_dte == null) return;

            foreach (var (category, page, item, original) in _savedPreferences)
            {
                if (original == null) continue;
                try
                {
                    _dte.Properties[category, page].Item(item).Value = original;
                    Console.Error.WriteLine(
                        $"[DEBUG] DTE option {category}.{page}.{item} restored to {original}");
                }
                catch { /* shutting down; best effort */ }
            }
            _savedPreferences.Clear();
        }

        /// <summary>
        /// Start a background thread that re-hides the main window whenever
        /// it observes it become visible. Modal dialogs (from TwinCAT, VS
        /// itself, or extensions) need a visible parent window; some of
        /// them bypass SuppressUI entirely and will promote our hidden
        /// MainWindow to attach. This thread snaps it back to invisible
        /// within ~500ms so the user doesn't see a frame flash.
        ///
        /// The watchdog only writes when the observed state is `visible` —
        /// it doesn't busy-write `Visible=false` on every tick.
        /// </summary>
        private void StartVisibilityWatchdog()
        {
            if (_visibilityWatchdog != null) return;
            _watchdogShouldStop = false;

            _visibilityWatchdog = new Thread(VisibilityWatchdogLoop)
            {
                IsBackground = true,
                Name = "DteVisibilityWatchdog",
            };
            _visibilityWatchdog.Start();
            Console.Error.WriteLine("[DEBUG] VisibilityWatchdog started");
        }

        private void VisibilityWatchdogLoop()
        {
            while (!_watchdogShouldStop)
            {
                try
                {
                    // Local copy — Close() may null out _dte concurrently.
                    var dte = _dte;
                    if (dte == null)
                    {
                        return;
                    }

                    var mainWin = dte.MainWindow;
                    if (mainWin != null && mainWin.Visible)
                    {
                        mainWin.Visible = false;
                        try { mainWin.WindowState = EnvDTE.vsWindowState.vsWindowStateMinimize; } catch { }
                        Console.Error.WriteLine("[DEBUG] VisibilityWatchdog re-hid the DTE main window");
                    }
                }
                catch
                {
                    // COM busy, RPC disconnected, or DTE torn down. Next
                    // tick will retry or the stop flag will exit.
                }

                // 500ms is fast enough that a briefly-raised window is
                // imperceptible, and slow enough that the cost is
                // negligible (two COM calls per tick in the idle case).
                Thread.Sleep(500);
            }
            Console.Error.WriteLine("[DEBUG] VisibilityWatchdog stopped");
        }

        private void StopVisibilityWatchdog()
        {
            _watchdogShouldStop = true;
            var t = _visibilityWatchdog;
            _visibilityWatchdog = null;
            if (t != null)
            {
                try { t.Join(2000); } catch { }
            }
        }

        /// <summary>
        /// Hide the DTE main window. Uses a short retry loop because
        /// MainWindow can throw RPC_E_SERVERCALL_RETRYLATER or return null
        /// while the owned shell is still coming up after process launch.
        /// Also tries to minimize as a belt-and-braces fallback if hiding
        /// ever fails silently on a given shell version.
        /// </summary>
        private void HideMainWindow()
        {
            if (_dte == null) return;

            const int maxAttempts = 10;
            for (int attempt = 1; attempt <= maxAttempts; attempt++)
            {
                try
                {
                    var mainWin = _dte.MainWindow;
                    if (mainWin != null)
                    {
                        mainWin.Visible = false;
                        try { mainWin.WindowState = EnvDTE.vsWindowState.vsWindowStateMinimize; } catch { }
                        Console.Error.WriteLine($"[DEBUG] DTE MainWindow hidden (attempt {attempt})");
                        return;
                    }
                }
                catch (Exception ex) when (attempt < maxAttempts)
                {
                    Console.Error.WriteLine($"[DEBUG] HideMainWindow attempt {attempt} transient: {ex.Message}");
                    Thread.Sleep(250);
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine($"[DEBUG] HideMainWindow failed after {attempt} attempts: {ex.Message}");
                    return;
                }
            }
        }

        /// <summary>Reads and verifies the actual selected engineering version, including after solution load.</summary>
        public string EffectiveTwinCATVersion
        {
            get
            {
                if (_dte == null) throw new InvalidOperationException("DTE not loaded.");
                var manager = (ITcRemoteManager)_dte.GetObject("TcRemoteManager");
                var actual = manager.Version;
                var requested = _forceTcVersion ?? _tcVersion;
                if (!string.Equals(actual, requested, StringComparison.Ordinal))
                    throw new InvalidOperationException($"Requested TwinCAT XAE {requested}, but effective version is {actual}.");
                return actual;
            }
        }

        private void LoadTwinCATVersion()
        {
            if (_dte == null) throw new InvalidOperationException("DTE not loaded.");
            var manager = (ITcRemoteManager)_dte.GetObject("TcRemoteManager");
            var available = new List<string>();
            foreach (string version in manager.Versions) available.Add(version);
            var actual = SelectExactTwinCATVersion(_forceTcVersion ?? _tcVersion, available,
                version => manager.Version = version, () => manager.Version);
            Console.Error.WriteLine($"[DEBUG] Verified TwinCAT XAE version: {actual}");
        }

        private static string SelectExactTwinCATVersion(string requested, IEnumerable<string> available,
            Action<string> select, Func<string> read)
        {
            if (string.IsNullOrWhiteSpace(requested) || !available.Contains(requested, StringComparer.Ordinal))
                throw new InvalidOperationException($"Requested TwinCAT XAE version '{requested}' is not installed; no fallback is permitted.");
            select(requested);
            var actual = read();
            if (!string.Equals(actual, requested, StringComparison.Ordinal))
                throw new InvalidOperationException($"Requested TwinCAT XAE {requested}, but effective version is {actual}.");
            return actual;
        }
    }
}
