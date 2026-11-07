using System;
using System.Collections;
using System.Collections.Generic;
using TcAutomation.Core;

static class BuildOutputTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    sealed class Panes : IEnumerable
    {
        public int FailAfter = int.MaxValue;
        public bool FailEnumerator;
        public IEnumerator GetEnumerator()
        {
            if (FailEnumerator) throw new InvalidCastException("collection");
            for (int i = 0; i < 4; i++)
            {
                if (i == FailAfter) throw new InvalidOperationException("enumerator");
                yield return i;
            }
        }
    }

    static KeyValuePair<string, string>? Read(object pane)
    {
        int i = (int)pane;
        if (i == 1) throw new NullReferenceException("TextDocument");
        if (i == 2) return null;
        return new KeyValuePair<string, string>("pane" + i, "text" + i);
    }

    public static void Run()
    {
        var all = BuildRun.Collect(() => new Panes(), Read);
        Check(all.Count == 3 && all["pane0"] == "text0" && all["pane3"] == "text3", "A failing or empty pane skips only that pane");
        Check(all[BuildRun.CaptureErrorsKey].Contains("pane unavailable: TextDocument"), "A failing pane is reported");
        var partial = BuildRun.Collect(() => new Panes { FailAfter = 1 }, Read);
        Check(partial.ContainsKey("pane0") && partial[BuildRun.CaptureErrorsKey].Contains("output capture failed: enumerator"),
            "An enumerator that fails keeps the panes read before it and is reported");
        Check(BuildRun.Collect(() => new Panes { FailEnumerator = true }, Read).Count == 1, "A collection that fails gives only its error and no exception");
        Check(BuildRun.Collect(() => throw new InvalidCastException("OutputWindow"), Read).ContainsKey(BuildRun.CaptureErrorsKey), "No output window: reported, no exception");

        var before = new Dictionary<string, string> { ["Ausgabe {1BD8A850-02D1-11D1-BEE7-00A0C913D1F8}"] = "old\n" };
        var after = new Dictionary<string, string> { ["Debug {2}"] = "debugger\n", ["Ausgabe {1BD8A850-02D1-11D1-BEE7-00A0C913D1F8}"] = "old\nerror C0001\n" };
        string text = BuildRun.Compose(before, after);
        Check(text.StartsWith("[Ausgabe {1BD8A850-02D1-11D1-BEE7-00A0C913D1F8}]\nerror C0001"), "Build pane first by GUID, with this build's lines only: " + text);

        string tail = "error C0002 at the end";
        var huge = new Dictionary<string, string> { ["Build {1}"] = new string('x', BuildRun.MaxOutputChars * 2) + tail, ["TwinCAT {3}"] = "twincat line", ["Debug {2}"] = new string('d', BuildRun.MaxOutputChars) };
        string capped = BuildRun.Compose(new Dictionary<string, string>(), huge);
        Check(capped.Length <= BuildRun.MaxOutputChars, $"Output fits the cap, markers included: {capped.Length}");
        Check(capped.Contains(tail) && capped.Contains("twincat line") && capped.Contains("earlier characters omitted"),
            "Every pane keeps its tail, a small pane is kept whole, and the cut is marked");
        var same = new Dictionary<string, string> { [BuildRun.CaptureErrorsKey] = "pane unavailable: X\n" };
        Check(BuildRun.Compose(same, new Dictionary<string, string>(same)).Contains("pane unavailable: X"), "A repeated capture error is still reported");
        var many = new Dictionary<string, string>();
        for (int i = 0; i < 1000; i++) many[new string('k', 80) + i] = "t";
        Check(BuildRun.Compose(new Dictionary<string, string>(), many).Length <= BuildRun.MaxOutputChars, "Headers and markers also fit the cap");
        Console.WriteLine("PASS: " + cases + " build output capture cases; no XAE or PLC used.");
    }
}
