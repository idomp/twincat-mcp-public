using System;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading;
using TcAutomation.Commands;
using TcAutomation.Core;
using TwinCAT.Ads;

static class ListOpsTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    static bool Throws(Action action) { try { action(); return false; } catch (ArgumentException) { return true; } }
    static JsonElement J(string json) => JsonDocument.Parse(json).RootElement;

    public static void Run()
    {
        // Reads need Run or Stop. Every other state refuses.
        Check(AdsValues.Readable(AdsState.Run) && AdsValues.Readable(AdsState.Stop), "Run and Stop allow a read");
        foreach (AdsState s in Enum.GetValues(typeof(AdsState)))
            if (s != AdsState.Run && s != AdsState.Stop)
                Check(!AdsValues.Readable(s), $"{s} refuses a read");

        // List write values keep their JSON form exactly.
        Check(AdsValues.ScalarText(J("\"3.14\"")) == "3.14", "A string stays as is");
        Check(AdsValues.ScalarText(J("42")) == "42", "An integer keeps its text");
        Check(AdsValues.ScalarText(J("18446744073709551615")) == "18446744073709551615", "A ULINT maximum is not rounded through a double");
        Check(AdsValues.ScalarText(J("-9223372036854775808")) == "-9223372036854775808", "A LINT minimum is not rounded");
        Check(AdsValues.ScalarText(J("0.1")) == "0.1", "A decimal keeps its text");
        Check(AdsValues.ScalarText(J("true")) == "TRUE" && AdsValues.ScalarText(J("false")) == "FALSE", "Booleans become TRUE and FALSE");
        Check(AdsValues.Boolean(AdsValues.ScalarText(J("true"))) && !AdsValues.Boolean(AdsValues.ScalarText(J("false"))), "Boolean text parses back");
        Check(Throws(() => AdsValues.ScalarText(J("null"))), "null is refused");
        Check(Throws(() => AdsValues.ScalarText(J("{}"))), "An object is refused");
        Check(Throws(() => AdsValues.ScalarText(J("[1]"))), "An array is refused");

        // Result text uses an invariant decimal point, whatever the PC culture.
        var culture = Thread.CurrentThread.CurrentCulture;
        try
        {
            Thread.CurrentThread.CurrentCulture = new CultureInfo("de-DE");
            Check(AdsValues.Text(3.5f) == "3.5" && AdsValues.Text(2.25d) == "2.25", "REAL and LREAL text keep a decimal point under de-DE");
            Check(AdsValues.Text(1234567) == "1234567", "An integer has no group separator under de-DE");
        }
        finally { Thread.CurrentThread.CurrentCulture = culture; }
        Check(AdsValues.Text(true) == "True" && AdsValues.Text("x") == "x" && AdsValues.Text(null) == "null", "Other values keep their 3.0 text");
        Check(AdsValues.Text(float.PositiveInfinity) == float.PositiveInfinity.ToString() && AdsValues.Text(double.NegativeInfinity) == double.NegativeInfinity.ToString()
              && AdsValues.Text(double.NaN) == double.NaN.ToString(), "Infinity and NaN keep their 3.0 text");

        // Negative zero keeps its sign through the write parser.
        Check(BitConverter.DoubleToInt64Bits((double)AdsValues.Parse("LREAL", 8, "-0.0")) == BitConverter.DoubleToInt64Bits(-0d), "LREAL -0.0 keeps its sign");
        Check(BitConverter.ToInt32(BitConverter.GetBytes((float)AdsValues.Parse("REAL", 4, "-0")), 0) == BitConverter.ToInt32(BitConverter.GetBytes(-0f), 0), "REAL -0 keeps its sign");
        Check(BitConverter.DoubleToInt64Bits((double)AdsValues.Parse("LREAL", 8, "0")) == 0L && (double)AdsValues.Parse("LREAL", 8, "-1.5") == -1.5, "Zero and negative numbers are unchanged");

        // Ordered items keep every requested read, duplicates included. Results keeps the last read.
        var r = new ReadVariableListResult { SymbolCount = 3 };
        r.Add("A", new ReadVariableItemResult { Success = true, Value = "1" });
        r.Add("B", new ReadVariableItemResult { Success = true, Value = "2" });
        r.Add("A", new ReadVariableItemResult { Success = true, Value = "3" });
        Check(r.Items.Count == 3 && r.Items.Select(i => i.Index).SequenceEqual(new[] { 0, 1, 2 }), "Items keep the request order and index");
        Check(r.Items.Select(i => i.Symbol).SequenceEqual(new[] { "A", "B", "A" }), "Items keep the duplicate symbol");
        Check(r.Items[0].Result.Value == "1" && r.Items[2].Result.Value == "3", "Each duplicate keeps its own value");
        Check(r.Results.Count == 2 && r.Results["A"].Value == "3", "Results keeps the last read of each symbol");
        Check(r.Success, "Duplicate reads that all succeed pass");

        var mixed = new ReadVariableListResult { SymbolCount = 2 };
        mixed.Add("A", new ReadVariableItemResult { Success = true });
        mixed.Add("A", new ReadVariableItemResult { Success = false, ErrorMessage = "x" });
        mixed.ErrorCount = 1;
        Check(!mixed.Success, "One failed item fails the receipt");
        var short1 = new ReadVariableListResult { SymbolCount = 2 };
        short1.Add("A", new ReadVariableItemResult { Success = true });
        Check(!short1.Success, "A missing item fails the receipt");

        // The serialized result names both fields, so the client sees the ordered items.
        var json = JsonSerializer.Serialize(r);
        Check(json.Contains("\"Items\":[{\"Index\":0,\"Symbol\":\"A\"") && json.Contains("\"Results\":{"), "Items and Results are both serialized");
        Console.WriteLine("PASS: " + cases + " list operation cases; no ADS or PLC used.");
    }
}
