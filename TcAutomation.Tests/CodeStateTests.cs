using System;
using TcAutomation.Commands;

static class CodeStateTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    // Reads return the given values in order, then repeat the last one. Each pause advances the clock
    // by stepMs. A loop that never pauses stops at the read cap instead of hanging the run.
    static (string state, int reads, long clock) Settle(string[] values, long settleMs = 10000, long stepMs = 250)
    {
        int reads = 0; long clock = 0;
        string Read()
        {
            if (reads >= 100) throw new Exception("Read cap reached: the settle loop does not stop");
            return values[Math.Min(reads++, values.Length - 1)];
        }
        var state = MatchingLoginCommand.SettledCodeState(Read, () => clock, () => clock += stepMs, settleMs);
        return (state, reads, clock);
    }

    public static void Run()
    {
        var set = Settle(new[] { "Code unchanged" });
        Check(set.state == "Code unchanged" && set.reads == 1 && set.clock == 0, "A set value returns at once, with no wait");

        var late = Settle(new[] { "", null, "No reference code generated" });
        Check(late.state == "No reference code generated" && late.reads == 3, "Empty and missing values are re-read until set");

        var wrong = Settle(new[] { "Code changed", "Code unchanged" });
        Check(wrong.state == "Code changed" && wrong.reads == 1, "A set but wrong value is not re-read");

        var lateWrong = Settle(new[] { "", "Code changed", "Code unchanged" });
        Check(lateWrong.state == "Code changed" && lateWrong.reads == 2, "The first set value after an empty one is final");

        var never = Settle(new[] { "" }, settleMs: 1000, stepMs: 250);
        Check(never.state == "" && never.clock == 1000 && never.reads == 5, "An empty value stops at the settle limit");

        var uneven = Settle(new[] { "" }, settleMs: 900, stepMs: 250);
        Check(uneven.clock == 1000 && uneven.reads == 5, "A limit between polls allows one last read past it");

        var missing = Settle(new string[] { null }, settleMs: 500, stepMs: 250);
        Check(missing.state == null && missing.clock == 500 && missing.reads == 3, "A missing property stops at the settle limit");

        var com = new InvalidOperationException("COM");
        int n = 0; Exception escaped = null;
        try { MatchingLoginCommand.SettledCodeState(() => n++ == 0 ? "" : throw com, () => n * 1000L, () => { }); }
        catch (Exception ex) { escaped = ex; }
        Check(ReferenceEquals(escaped, com) && n == 2, "A read error inside the wait escapes unchanged");

        string Refusal(string s, bool before) => MatchingLoginCommand.CodeStateRefusal(s, before);
        Check(Refusal("Code unchanged", true) == null && Refusal("Code unchanged", false) == null, "Code unchanged passes before and after login");
        Check(Refusal("No reference code generated", true) == null, "No reference code passes before login");
        Check(Refusal("No reference code generated", false) != null, "No reference code refuses after login");
        Check(Refusal("Code changed", true)?.EndsWith("CodeState: Code changed") == true, "A mismatch refuses and names the state");
        Check(Refusal("", true)?.EndsWith("CodeState: (empty after 10 s)") == true &&
              Refusal(null, false)?.EndsWith("CodeState: (empty after 10 s)") == true, "Empty and missing refuse and name the wait");
        Console.WriteLine("PASS: " + cases + " CodeState settle and policy cases; no XAE or PLC used.");
    }
}
