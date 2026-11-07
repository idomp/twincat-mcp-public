using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Threading;
using System.Windows.Forms;
using EnvDTE;

namespace TcAutomation.Core
{
    public sealed class OwnedDeadline : IDisposable
    {
        readonly System.Threading.Timer timer;
        int state;
        public bool Expired => Volatile.Read(ref state) == 2;
        public OwnedDeadline(Action terminateOwned, int seconds)
        {
            if (seconds < 1 || seconds > 600) throw new ArgumentOutOfRangeException(nameof(seconds));
            timer = new System.Threading.Timer(_ =>
            {
                if (Interlocked.CompareExchange(ref state, 2, 0) == 0) terminateOwned();
            }, null, seconds * 1000, Timeout.Infinite);
        }
        public void Complete()
        {
            if (Interlocked.CompareExchange(ref state, 1, 0) == 2) throw new TimeoutException("Operation deadline expired; outcome is unknown.");
            timer.Dispose();
        }
        public void Dispose() { Interlocked.CompareExchange(ref state, 1, 0); timer.Dispose(); }
    }

    public sealed class BuildRun
    {
        public string BuildId { get; set; } = Guid.NewGuid().ToString("N");
        public DateTime StartedUtc { get; set; }
        public DateTime CompletedUtc { get; set; }
        public long SourceRevision { get; set; }
        public int FailedProjects { get; set; }
        [System.Text.Json.Serialization.JsonIgnore] public string Output { get; set; } = "";
        public string Configuration { get; set; }
        public string Platform { get; set; }

        // Kept separate from COM for deterministic completion/output tests.
        public static string OutputDelta(string before, string after)
        {
            if (after.StartsWith(before, StringComparison.Ordinal)) return after.Substring(before.Length);
            return after; // The IDE cleared/replaced the pane during this build.
        }

        public static BuildRun Execute(VisualStudioInstance vs, int timeoutSeconds)
        {
            if (timeoutSeconds < 1 || timeoutSeconds > 600) throw new ArgumentOutOfRangeException(nameof(timeoutSeconds));
            var solution = vs.Dte.Solution.SolutionBuild;
            if (solution.BuildState == vsBuildState.vsBuildStateInProgress)
                throw new InvalidOperationException("An earlier build is still running; no second build was dispatched.");
            var configuration = (EnvDTE80.SolutionConfiguration2)solution.ActiveConfiguration;
            var run = new BuildRun { SourceRevision = vs.SourceRevision, Configuration = configuration.Name, Platform = configuration.PlatformName };
            var events = vs.Dte.Events.BuildEvents;
            bool started = false, done = false;
            _dispBuildEvents_OnBuildBeginEventHandler begin = (scope, action) => { if (action == vsBuildAction.vsBuildActionBuild) started = true; };
            _dispBuildEvents_OnBuildDoneEventHandler end = (scope, action) => { if (started && action == vsBuildAction.vsBuildActionBuild) done = true; };
            var before = CapturePanes(vs, true);
            var elapsed = Stopwatch.StartNew();
            bool deadlineExpired = false;
            int completionState = 0;
            // Also bounds a COM call that never returns to the STA loop.
            using var deadline = new System.Threading.Timer(_ =>
            {
                if (Interlocked.CompareExchange(ref completionState, 2, 0) != 0) return;
                deadlineExpired = true;
                vs.KillOwnedProcess("build deadline expired");
            }, null, timeoutSeconds * 1000, Timeout.Infinite);
            events.OnBuildBegin += begin;
            events.OnBuildDone += end;
            try
            {
                run.StartedUtc = DateTime.UtcNow;
                solution.Build(false);
                while (!done)
                {
                    if (deadlineExpired || elapsed.Elapsed.TotalSeconds >= timeoutSeconds)
                        throw new TimeoutException("Build completion was not observed before the deadline; outcome is unknown.");
                    // DTE events are delivered to this STA. SpinWait alone does
                    // not pump them, and Done may still describe an earlier build.
                    Application.DoEvents();
                    System.Threading.Thread.Sleep(25);
                }
                if (Interlocked.CompareExchange(ref completionState, 1, 0) != 0)
                    throw new TimeoutException("Build deadline expired; outcome is unknown.");
                run.CompletedUtc = DateTime.UtcNow;
                run.FailedProjects = solution.LastBuildInfo;
                run.Output = Compose(before, CapturePanes(vs));
                return run;
            }
            catch (Exception ex) when (deadlineExpired)
            {
                throw new TimeoutException("Build deadline expired; the owned shell was terminated and the outcome is unknown.", ex);
            }
            finally
            {
                Interlocked.CompareExchange(ref completionState, 1, 0);
                try { events.OnBuildBegin -= begin; events.OnBuildDone -= end; } catch { }
                GC.KeepAlive(events);
            }
        }

        public const int MaxOutputChars = 65536;
        public const string CaptureErrorsKey = "capture errors";
        const string BuildPaneGuid = "{1BD8A850-02D1-11D1-BEE7-00A0C913D1F8}";  // vsOutputWindowPaneGuid Build, not localized

        static Dictionary<string, string> CapturePanes(VisualStudioInstance vs, bool clear = false) =>
            Collect(() => vs.Dte.ToolWindows.OutputWindow.OutputWindowPanes, item =>
            {
                var pane = (OutputWindowPane)item;
                if (clear) pane.Clear();
                var document = pane.TextDocument;
                if (document == null) return null;
                // A pane whose name or GUID cannot be read still keeps its text.
                string name, guid;
                try { name = pane.Name; } catch (Exception) { name = "pane"; }
                try { guid = pane.Guid; } catch (Exception) { guid = "{guid unavailable}"; }  // the same key in both passes
                return new KeyValuePair<string, string>(name + " " + guid, document.StartPoint.CreateEditPoint().GetText(document.EndPoint));
            });

        // Diagnostics never fail a build: the collection, the enumerator and each pane can throw.
        // Partial output is kept, and each failure is reported under CaptureErrorsKey.
        public static Dictionary<string, string> Collect(Func<System.Collections.IEnumerable> panes, Func<object, KeyValuePair<string, string>?> read)
        {
            var result = new Dictionary<string, string>();
            var errors = new List<string>();
            try
            {
                foreach (var pane in panes())
                {
                    try
                    {
                        var text = read(pane);
                        if (text.HasValue) result[text.Value.Key] = text.Value.Value;
                    }
                    catch (Exception ex) { errors.Add("pane unavailable: " + ex.Message); }
                }
            }
            catch (Exception ex) { errors.Add("output capture failed: " + ex.Message); }
            if (errors.Count > 0) result[CaptureErrorsKey] = string.Join("\n", errors) + "\n";
            return result;
        }

        static bool IsBuildPane(string key) =>
            key.IndexOf(BuildPaneGuid, StringComparison.OrdinalIgnoreCase) >= 0
            || key.IndexOf("build", StringComparison.OrdinalIgnoreCase) >= 0
            || key.IndexOf("twincat", StringComparison.OrdinalIgnoreCase) >= 0;

        // Build panes first. Over the cap, every pane gets a fair share and keeps its tail, where the compiler
        // writes its last error. Markers count toward the cap.
        public static string Compose(Dictionary<string, string> before, Dictionary<string, string> after)
        {
            var panes = after.Where(p => p.Key != CaptureErrorsKey)
                .Select(p => new { Head = "[" + p.Key + "]\n", Text = OutputDelta(before.TryGetValue(p.Key, out var old) ? old : "", p.Value) })
                .Where(p => !string.IsNullOrWhiteSpace(p.Text))
                .OrderBy(p => IsBuildPane(p.Head) ? 0 : 1)
                .ToList();
            // Capture errors are reported as they are, from both passes; a delta would hide a repeated one.
            string errors = (before.TryGetValue(CaptureErrorsKey, out var e1) ? "before the build: " + e1 : "")
                          + (after.TryGetValue(CaptureErrorsKey, out var e2) ? e2 : "");
            if (errors.Length > 0) panes.Insert(0, new { Head = "[" + CaptureErrorsKey + "]\n", Text = errors.Length > 4096 ? errors.Substring(0, 4096) + "\n" : errors });
            int whole = panes.Sum(p => p.Head.Length + p.Text.Length) + Math.Max(0, panes.Count - 1);
            if (whole <= MaxOutputChars) return string.Join("\n", panes.Select(p => p.Head + p.Text));

            const int Marker = 48;  // room for "[N earlier characters omitted]\n"
            int budget = MaxOutputChars - Math.Max(0, panes.Count - 1);
            var keep = new int[panes.Count];
            var open = Enumerable.Range(0, panes.Count).ToList();
            while (open.Count > 0)
            {
                int share = budget / open.Count;
                var fits = open.Where(i => panes[i].Head.Length + panes[i].Text.Length <= share).ToList();
                if (fits.Count == 0)
                {
                    foreach (var i in open) keep[i] = Math.Max(0, share - panes[i].Head.Length - Marker);
                    break;
                }
                foreach (var i in fits) { keep[i] = panes[i].Text.Length; budget -= panes[i].Head.Length + panes[i].Text.Length; open.Remove(i); }
            }
            var parts = panes.Select((p, i) => keep[i] >= p.Text.Length ? p.Head + p.Text
                : p.Head + $"[{p.Text.Length - keep[i]} earlier characters omitted]\n" + p.Text.Substring(p.Text.Length - keep[i])).ToList();
            // Headers and markers alone can exceed the cap with many panes: drop trailing panes, which sort last.
            int dropped = 0;
            const int Tail = 40;  // room for "\n[N more panes omitted]"
            while (parts.Count > 1 && parts.Sum(s => s.Length) + parts.Count - 1 + (dropped > 0 ? Tail : 0) > MaxOutputChars) { parts.RemoveAt(parts.Count - 1); dropped++; }
            var text = string.Join("\n", parts) + (dropped > 0 ? $"\n[{dropped} more panes omitted]" : "");
            return text.Length <= MaxOutputChars ? text : text.Substring(0, MaxOutputChars);
        }
    }
}
