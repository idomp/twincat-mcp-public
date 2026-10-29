using System;
using System.Diagnostics;
using System.Text.Json;
using System.Threading;
using TwinCAT.Ads;

namespace TcAutomation.Commands
{
    /// <summary>
    /// Sets the TwinCAT runtime state (Run, Stop, Config, Reset, Reconfig) via direct ADS connection.
    /// Note: Remote state changes may be limited depending on target device configuration.
    /// For full control, use the restart command with Automation Interface.
    /// </summary>
    public static class SetStateCommand
    {
        const int BudgetMs = 30000;
        const int PollMs = 500;
        const int ReadTimeoutMs = 1000;
        const int MinReadMs = 100;
        const int DefaultTimeoutMs = 5000;
        // A PLC Run write needs time for one read and a poll after it, and a reply to itself.
        const int WriteReserveMs = ReadTimeoutMs + PollMs + ReadTimeoutMs;
        // It also needs the system read and the writer's read before it.
        const int StartReserveMs = 2 * ReadTimeoutMs + WriteReserveMs;

        public static int Execute(string amsNetId, int port, string targetState)
        {
            var result = new SetStateResult
            {
                AmsNetId = amsNetId,
                Port = port,
                CheckedPort = port,
                RequestedState = targetState
            };
            int Emit(int code)
            {
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return code;
            }

            try
            {
                if (!TryParseState(targetState, out AdsState adsState))
                {
                    result.ErrorMessage = $"Invalid state '{targetState}'. Valid states: Run, Stop, Config, Reset, Reconfig";
                    return Emit(1);
                }

                AdsState? systemState = null;
                if (IsPlcPort(port) && adsState == AdsState.Run)
                {
                    using var system = new AdsClient();
                    system.Connect(amsNetId, SystemPort);
                    systemState = system.ReadState().AdsState;
                }
                int writePort = ChooseWritePort(port, adsState, systemState);
                int checkPort = CheckPort(port, adsState);
                result.Port = writePort;
                result.CheckedPort = checkPort;

                // The default client fails requests for 21 s after a timeout or a missing port.
                using var probe = new AdsClient(AdsClientSettings.FastWriteThrough);
                probe.Connect(amsNetId, checkPort);
                probe.Timeout = DefaultTimeoutMs;
                AdsState? before;
                try { before = probe.ReadState().AdsState; }
                catch (AdsErrorException ex) when (ex.ErrorCode == AdsErrorCode.TargetPortNotFound)
                {
                    before = null; // no state was read
                }
                result.PreviousState = before?.ToString();
                if (before == adsState)
                {
                    result.CurrentState = before.ToString();
                    result.StateDescription = GetStateDescription(adsState);
                    result.Success = true;
                    result.Warning = "Already in requested state";
                    return Emit(0);
                }

                bool sent = false;
                AdsState found;
                try
                {
                    found = WriteState(amsNetId, writePort, adsState, DefaultTimeoutMs, null, ref sent);
                }
                catch (AdsErrorException ex) when (ex.ErrorCode == AdsErrorCode.DeviceServiceNotSupported)
                {
                    result.Dispatched = sent; // Emit runs before the finally
                    result.ErrorMessage = $"State change not supported via ADS on this target. Use 'twincat_restart' (which uses Automation Interface) for remote state changes, or control the target locally.";
                    return Emit(1);
                }
                finally { result.Dispatched = sent; }
                if (!sent && writePort == checkPort)
                {
                    result.CurrentState = found.ToString();
                    result.StateDescription = GetStateDescription(found);
                    result.Success = true;
                    result.Warning = "Already in requested state";
                    return Emit(0);
                }

                string? readError;
                if (!Verifiable(adsState))
                {
                    // The end state of Reset and Reconfig is not checked.
                    Thread.Sleep(PollMs);
                    var now = TryRead(probe, amsNetId, checkPort, ReadTimeoutMs, out readError);
                    result.CurrentState = now?.ToString();
                    result.StateDescription = now.HasValue ? GetStateDescription(now.Value) : "";
                    result.OutcomeUnknown = true;
                    result.ErrorMessage = $"{adsState} was written to port {writePort}; its end state is not checked. Port {checkPort} reports {(now?.ToString() ?? "no state: " + readError)}.";
                    return Emit(1);
                }

                // Run from Config starts the system; the PLC can then come up in Stop.
                bool startPlc = adsState == AdsState.Run && writePort != checkPort;
                using var systemProbe = startPlc ? new AdsClient(AdsClientSettings.FastWriteThrough) : null;
                systemProbe?.Connect(amsNetId, SystemPort);
                string? systemError = null, startError = null;
                AdsState? last = null, seen = null;
                bool readSinceWrite = false;
                readError = null;
                var clock = Stopwatch.StartNew();
                long Left() => BudgetMs - clock.ElapsedMilliseconds;
                while (true)
                {
                    Thread.Sleep((int)Math.Max(0, Math.Min(PollMs, Left())));
                    long left = Left();
                    if (left < MinReadMs) break;
                    last = TryRead(probe, amsNetId, checkPort, Math.Min(ReadTimeoutMs, left), out readError);
                    readSinceWrite = true;
                    seen = last ?? seen;
                    if (last == adsState) break;
                    if (!startPlc || last != AdsState.Stop || Left() < StartReserveMs) continue;
                    if (TryRead(systemProbe!, amsNetId, SystemPort, ReadTimeoutMs, out systemError) != AdsState.Run) continue;
                    // A write that entered WriteControl is never tried again.
                    bool plcSent = false;
                    AdsState? plcNow = null;
                    startError = null;
                    try { plcNow = WriteState(amsNetId, checkPort, AdsState.Run, ReadTimeoutMs, Left, ref plcSent); }
                    catch (TwinCAT.AdsException ex) { startError = (plcSent ? "the Run write failed: " : "the connect or read before the Run write failed: ") + ex.Message; }
                    finally { result.PlcRunDispatched |= plcSent; }
                    if (plcNow == adsState) { last = plcNow; break; } // the writer read Run and wrote nothing
                    if (plcNow.HasValue && !plcSent) startError = $"the Run write was skipped: under {WriteReserveMs} ms was left.";
                    if (plcSent) { startPlc = false; last = seen = null; readSinceWrite = false; }
                }

                result.CurrentState = last?.ToString();
                result.StateDescription = last.HasValue ? GetStateDescription(last.Value) : "";
                if (last != adsState)
                {
                    result.OutcomeUnknown = result.Dispatched || result.PlcRunDispatched;
                    string observed = last.HasValue ? $" It reports {last}."
                        : !readSinceWrite ? " No read completed after the last write attempt."
                        : $" Its last read failed with {readError}; last state seen: {seen?.ToString() ?? "none"}.";
                    string system = systemProbe == null ? ""
                        : $" Port {SystemPort} reports {(TryRead(systemProbe, amsNetId, SystemPort, ReadTimeoutMs, out systemError)?.ToString() ?? "no state: " + systemError)}.";
                    result.ErrorMessage = $"Port {checkPort} did not report {adsState} within {clock.Elapsed.TotalSeconds:0} s" +
                        (sent ? $" of the write to port {writePort}." : ".") +
                        (result.PlcRunDispatched ? $" Run was then written to port {checkPort}." : "") + observed + system +
                        (startError != null ? $" On port {checkPort}, {startError}" : "");
                    return Emit(1);
                }
                result.Success = true;
                if (result.PlcRunDispatched) result.Warning = $"Port {checkPort} came up in Stop; Run was then written to it.";
                return Emit(0);
            }
            catch (AdsErrorException ex)
            {
                result.ErrorMessage = $"ADS Error: {ex.ErrorCode} - {ex.Message}";
                result.OutcomeUnknown = result.Dispatched || result.PlcRunDispatched;
                return Emit(1);
            }
            catch (Exception ex)
            {
                result.ErrorMessage = ex.Message;
                result.OutcomeUnknown = result.Dispatched || result.PlcRunDispatched;
                return Emit(1);
            }
        }

        // Returns the state read before the write. Writes nothing when the port already
        // reports the state, or when less than WriteReserveMs is left.
        static AdsState WriteState(string amsNetId, int port, AdsState state, int timeoutMs, Func<long>? left, ref bool sent)
        {
            using var writer = new AdsClient();
            writer.Connect(amsNetId, port);
            writer.Timeout = timeoutMs;
            var current = writer.ReadState();
            if (current.AdsState == state || (left != null && left() < WriteReserveMs)) return current.AdsState;
            // Set before the call: the target can apply a write whose reply is lost.
            sent = true;
            writer.WriteControl(new StateInfo(state, current.DeviceState));
            return current.AdsState;
        }

        // Null when no state was read. Ports drop while TwinCAT restarts.
        static AdsState? TryRead(AdsClient client, string amsNetId, int port, long timeoutMs, out string? error)
        {
            client.Timeout = (int)Math.Max(1, timeoutMs);
            try
            {
                var code = client.TryReadState(out StateInfo info);
                error = code == AdsErrorCode.NoError ? null : code.ToString();
                return error == null ? info.AdsState : (AdsState?)null;
            }
            catch (TwinCAT.ClientNotConnectedException ex)
            {
                error = ex.Message;
                try { client.Connect(amsNetId, port); }
                catch (TwinCAT.AdsException reconnect) { error += " Reconnect failed: " + reconnect.Message; }
                return null;
            }
        }

        public const int SystemPort = 10000;

        public static bool IsPlcPort(int port) => port >= 851 && port <= 899;

        static bool Verifiable(AdsState state) => state == AdsState.Run || state == AdsState.Stop || state == AdsState.Config;

        static bool IsSystemState(AdsState state) => state == AdsState.Config || state == AdsState.Reconfig;

        /// <summary>
        /// On a PLC port, Config and Reconfig go to the System Service. Run goes there only
        /// while the system is not in Run; in Run, the PLC port takes the write.
        /// </summary>
        public static int ChooseWritePort(int port, AdsState requested, AdsState? systemState)
        {
            if (!IsPlcPort(port)) return port;
            if (IsSystemState(requested)) return SystemPort;
            if (requested == AdsState.Run && systemState != AdsState.Run) return SystemPort;
            return port;
        }

        /// <summary>The port whose state answers the request: on a PLC port, the System Service for Config and Reconfig.</summary>
        public static int CheckPort(int port, AdsState requested) =>
            IsPlcPort(port) && IsSystemState(requested) ? SystemPort : port;

        private static bool TryParseState(string state, out AdsState adsState)
        {
            adsState = AdsState.Invalid;
            
            switch (state.ToLowerInvariant())
            {
                case "run":
                case "running":
                    adsState = AdsState.Run;
                    return true;
                case "stop":
                case "stopped":
                    adsState = AdsState.Stop;
                    return true;
                case "config":
                case "configuration":
                    adsState = AdsState.Config;
                    return true;
                case "reset":
                    adsState = AdsState.Reset;
                    return true;
                case "reconfig":
                    adsState = AdsState.Reconfig;
                    return true;
                default:
                    return false;
            }
        }

        private static string GetStateDescription(AdsState state)
        {
            switch (state)
            {
                case AdsState.Invalid: return "Invalid state";
                case AdsState.Idle: return "Idle - System idle";
                case AdsState.Reset: return "Reset - System reset";
                case AdsState.Init: return "Init - Initializing";
                case AdsState.Start: return "Start - Starting up";
                case AdsState.Run: return "Run - Running normally 🟢";
                case AdsState.Stop: return "Stop - Stopped 🔴";
                case AdsState.SaveConfig: return "SaveConfig - Saving configuration";
                case AdsState.LoadConfig: return "LoadConfig - Loading configuration";
                case AdsState.PowerFailure: return "PowerFailure - Power failure detected";
                case AdsState.PowerGood: return "PowerGood - Power restored";
                case AdsState.Error: return "Error - Error state ⚠️";
                case AdsState.Shutdown: return "Shutdown - Shutting down";
                case AdsState.Suspend: return "Suspend - Suspended";
                case AdsState.Resume: return "Resume - Resuming";
                case AdsState.Config: return "Config - Configuration mode 🔧";
                case AdsState.Reconfig: return "Reconfig - Reconfiguring";
                default: return $"Unknown state: {state}";
            }
        }
    }

    public class SetStateResult
    {
        public string AmsNetId { get; set; } = "";
        // The port selected for the first write; Dispatched says whether its WriteControl call was entered.
        public int Port { get; set; }
        // The port selected for read-back.
        public int CheckedPort { get; set; }
        public string RequestedState { get; set; } = "";
        // Null when no state was read.
        public string? PreviousState { get; set; }
        public string? CurrentState { get; set; }
        public string StateDescription { get; set; } = "";
        public bool Success { get; set; }
        public bool Dispatched { get; set; }
        // WriteControl(Run) on CheckedPort was entered after the system started.
        public bool PlcRunDispatched { get; set; }
        public bool OutcomeUnknown { get; set; }
        public string? Warning { get; set; }
        public string? ErrorMessage { get; set; }
    }
}
