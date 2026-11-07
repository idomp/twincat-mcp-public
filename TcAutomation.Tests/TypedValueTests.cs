using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using TcAutomation.Core;

static class TypedValueTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }
    static bool Rejects(Func<object> action, string fragment = null)
    {
        try { action(); return false; } catch (ArgumentException ex) { return fragment == null || ex.Message.Contains(fragment); }
    }
    static TypeNode P(string name, int size) => new TypeNode { Kind = TypeKind.Primitive, Name = name, Primitive = name, Size = size };
    static string[] Cells(ChannelPlan plan, byte[] payload, out List<string> errors, out int unknown)
    {
        var e = new List<string>(); int u = 0;
        var cells = plan.Columns.Select(c => { var s = TypedValues.Cell(c, payload, 0, out var err, out var unk); if (err != null) e.Add(err); if (unk) u++; return s; }).ToArray();
        errors = e; unknown = u; return cells;
    }

    // The v3 formatting for the 15 numeric types, so scalar CSV stays byte-identical.
    static string V3(string type, byte[] b)
    {
        double v;
        switch (type)
        {
            case "LREAL": v = BitConverter.ToDouble(b, 0); break;
            case "REAL": v = (double)BitConverter.ToSingle(b, 0); break;
            case "LINT": v = (double)BitConverter.ToInt64(b, 0); break;
            case "DINT": v = (double)BitConverter.ToInt32(b, 0); break;
            case "INT": v = (double)BitConverter.ToInt16(b, 0); break;
            case "SINT": v = (double)(sbyte)b[0]; break;
            case "ULINT": case "LWORD": v = (double)BitConverter.ToUInt64(b, 0); break;
            case "UDINT": case "DWORD": v = (double)BitConverter.ToUInt32(b, 0); break;
            case "UINT": case "WORD": v = (double)BitConverter.ToUInt16(b, 0); break;
            case "USINT": case "BYTE": v = (double)b[0]; break;
            case "BOOL": v = b[0] != 0 ? 1.0 : 0.0; break;
            default: throw new ArgumentException(type);
        }
        return v.ToString("G", CultureInfo.InvariantCulture);
    }

    public static void Run()
    {
        var sizes = new Dictionary<string, int> { ["BOOL"] = 1, ["BYTE"] = 1, ["USINT"] = 1, ["SINT"] = 1, ["WORD"] = 2, ["UINT"] = 2, ["INT"] = 2,
            ["DWORD"] = 4, ["UDINT"] = 4, ["DINT"] = 4, ["REAL"] = 4, ["LWORD"] = 8, ["ULINT"] = 8, ["LINT"] = 8, ["LREAL"] = 8 };
        var patterns = new[] { 0x00, 0xFF, 0x7F, 0x80, 0x01, 0x55 };
        foreach (var kv in sizes)
            foreach (var p in patterns)
                foreach (var last in new[] { 0x00, 0x7F, 0x80, 0xFF, 0x43, 0x47 })
                {
                    var b = Enumerable.Repeat((byte)p, kv.Value).ToArray(); b[kv.Value - 1] = (byte)last;
                    var plan = TypedValues.Compile("MAIN.x", P(kv.Key, kv.Value));
                    Check(Cells(plan, b, out _, out _)[0] == V3(kv.Key, b), $"{kv.Key} {BitConverter.ToString(b)} keeps the v3 text");
                }
        var big = BitConverter.GetBytes((ulong)(1UL << 53) + 1);
        Check(Cells(TypedValues.Compile("x", P("ULINT", 8)), big, out _, out _)[0] == V3("ULINT", big), "ULINT above 2^53 keeps the v3 rounding");

        // Aliases are resolved by the adapter; the plan keeps the declared name.
        var alias = P("LREAL", 8); alias.Name = "T_Temperature";
        var ac = TypedValues.Compile("MAIN.t", alias).Columns.Single();
        Check(ac.Kind == LeafKind.Numeric && ac.DeclaredType == "T_Temperature", "Alias of a numeric gives a numeric column");

        // TIME family: exact integers in documented units, never through double.
        Check(Cells(TypedValues.Compile("t", P("TIME", 4)), BitConverter.GetBytes(uint.MaxValue), out _, out _)[0] == "4294967295", "TIME is integer ms up to UDINT max");
        Check(TypedValues.Compile("t", P("TIME", 4)).Columns[0].Unit == "ms", "TIME unit is ms");
        Check(Cells(TypedValues.Compile("t", P("LTIME", 8)), BitConverter.GetBytes(ulong.MaxValue), out _, out _)[0] == "18446744073709551615", "LTIME keeps all 64 bits");
        Check(Cells(TypedValues.Compile("t", P("LTIME", 8)), BitConverter.GetBytes((1UL << 53) + 1), out _, out _)[0] == "9007199254740993", "LTIME is exact above 2^53");
        Check(TypedValues.Compile("d", P("DATE", 4)).Columns[0].Unit == "s_since_1970-01-01" && TypedValues.Compile("d", P("TOD", 4)).Columns[0].Unit == "ms_since_midnight", "DATE in seconds, TOD in ms");
        Check(Rejects(() => TypedValues.Compile("t", P("TIME", 8))), "A TIME of the wrong size is rejected");

        // Enums: value and name columns, negatives, gaps, duplicate names, unknown values.
        var e = new TypeNode { Kind = TypeKind.Enum, Name = "E_Mode", Primitive = "INT", Size = 2 };
        e.EnumValues.Add(("Idle", BitConverter.GetBytes((short)0)));
        e.EnumValues.Add(("Fault", BitConverter.GetBytes((short)-5)));
        e.EnumValues.Add(("Run", BitConverter.GetBytes((short)10)));
        e.EnumValues.Add(("Running", BitConverter.GetBytes((short)10)));
        var ep = TypedValues.Compile("MAIN.mode", e);
        Check(ep.Columns.Select(c => c.Name).SequenceEqual(new[] { "MAIN.mode.value", "MAIN.mode.name" }), "Enum gives .value and .name");
        var ec = Cells(ep, BitConverter.GetBytes((short)-5), out _, out var u0);
        Check(ec[0] == "-5" && ec[1] == "\"Fault\"" && u0 == 0, "Negative enum value and its name");
        Check(Cells(ep, BitConverter.GetBytes((short)10), out _, out _)[1] == "\"Run|Running\"", "Names sharing a value in declared order");
        var unknown = Cells(ep, BitConverter.GetBytes((short)7), out _, out var u1);
        Check(unknown[0] == "7" && unknown[1] == "\"\"" && u1 == 1, "An undeclared value keeps its integer, an empty name and a count");
        var ue = new TypeNode { Kind = TypeKind.Enum, Name = "E_Big", Primitive = "ULINT", Size = 8 };
        ue.EnumValues.Add(("Max", BitConverter.GetBytes(ulong.MaxValue)));
        Check(Cells(TypedValues.Compile("m", ue), BitConverter.GetBytes(ulong.MaxValue), out _, out _)[0] == "18446744073709551615", "A 64-bit enum value is exact");
        Check(Rejects(() => TypedValues.Compile("m", new TypeNode { Kind = TypeKind.Enum, Name = "E_Bad", Primitive = "REAL", Size = 4 })), "An enum without an integer base is rejected");

        // Strings: quoting, formula escape, terminator, encodings.
        var s = new TypeNode { Kind = TypeKind.String, Name = "STRING(10)", Size = 11, CodePage = 1252 };
        var sp = TypedValues.Compile("MAIN.s", s);
        byte[] Str(string text, int size, Encoding enc) { var b = new byte[size]; var t = enc.GetBytes(text); Array.Copy(t, b, t.Length); return b; }
        Check(Cells(sp, Str("ready, \"A\"", 11, Encoding.GetEncoding(1252)), out _, out _)[0] == "\"ready, \"\"A\"\"\"", "Quotes doubled, comma kept");
        Check(Cells(sp, Str("=RUN", 11, Encoding.GetEncoding(1252)), out _, out _)[0] == "\"'=RUN\"", "Formula prefix escaped in text");
        Check(Cells(sp, Str("", 11, Encoding.GetEncoding(1252)), out _, out _)[0] == "\"\"", "Empty string");
        Check(Cells(sp, Str("café", 11, Encoding.GetEncoding(1252)), out _, out _)[0] == "\"café\"", "Single-byte non-ASCII through the code page");
        var full = Enumerable.Repeat((byte)'x', 11).ToArray();
        var fc = Cells(sp, full, out var se, out _);
        Check(fc[0] == "\"#DECODE_ERROR:unterminated_string\"" && se.Single() == "unterminated_string", "A missing terminator is a decode error");
        var u8 = new TypeNode { Kind = TypeKind.String, Name = "STRING(10)", Size = 11, CodePage = 65001 };
        Check(Cells(TypedValues.Compile("s", u8), Str("üא", 11, new UTF8Encoding(false)), out _, out _)[0] == "\"üא\"", "UTF-8 string");
        var bad = new byte[11]; bad[0] = 0xC3; bad[1] = 0x28;
        Check(Cells(TypedValues.Compile("s", u8), bad, out var be, out _)[0].Contains("invalid_encoding") && be.Count == 1, "Invalid UTF-8 is a decode error");
        var w = new TypeNode { Kind = TypeKind.String, Name = "WSTRING(4)", Size = 10, CodePage = 1200 };
        Check(Cells(TypedValues.Compile("w", w), Str("אé", 10, Encoding.Unicode), out _, out _)[0] == "\"אé\"", "WSTRING non-ASCII");
        var wodd = new byte[10]; wodd[0] = 0x41; wodd[1] = 0x00; wodd[2] = 0x00; wodd[3] = 0x42;  // zero byte at an odd position is not a terminator
        Check(Cells(TypedValues.Compile("w", w), wodd, out _, out _)[0] == "\"A\u4200\"", "WSTRING terminator is found only on code-unit boundaries");
        Check(Rejects(() => TypedValues.Compile("w", new TypeNode { Kind = TypeKind.String, Name = "WSTRING", Size = 9, CodePage = 1200 })), "An odd WSTRING size is rejected");

        // Structs: offsets, padding, nesting, ordering, overlap.
        var inner = new TypeNode { Kind = TypeKind.Struct, Name = "ST_In", Size = 8 };
        inner.Members.Add(("t", 0, P("REAL", 4)));
        inner.Members.Add(("ok", 4, P("BOOL", 1)));
        var outer = new TypeNode { Kind = TypeKind.Struct, Name = "ST_Out", Size = 24 };
        outer.Members.Add(("inner", 8, inner));
        outer.Members.Add(("mode", 0, e));
        outer.Members.Add(("v", 16, P("LREAL", 8)));
        var op = TypedValues.Compile("MAIN.s", outer);
        Check(op.Columns.Select(c => c.Name).SequenceEqual(new[] { "MAIN.s.mode.value", "MAIN.s.mode.name", "MAIN.s.inner.t", "MAIN.s.inner.ok", "MAIN.s.v" }), "Struct columns by offset, nested");
        Check(op.Columns.Select(c => c.Offset).SequenceEqual(new[] { 0, 0, 8, 12, 16 }), "Member offsets include the padding");
        var ob = new byte[24]; BitConverter.GetBytes((short)10).CopyTo(ob, 0); BitConverter.GetBytes(2.5f).CopyTo(ob, 8); ob[12] = 1; BitConverter.GetBytes(-1.25).CopyTo(ob, 16);
        Check(string.Join(",", Cells(op, ob, out _, out _)) == "10,\"Run|Running\",2.5,1,-1.25", "Struct row decodes each member");
        var overlap = new TypeNode { Kind = TypeKind.Struct, Name = "ST_X", Size = 4 };
        overlap.Members.Add(("a", 0, P("DINT", 4))); overlap.Members.Add(("b", 2, P("INT", 2)));
        Check(Rejects(() => TypedValues.Compile("x", overlap), "overlaps"), "Overlapping members are rejected");
        var outside = new TypeNode { Kind = TypeKind.Struct, Name = "ST_Y", Size = 4 };
        outside.Members.Add(("a", 2, P("DINT", 4)));
        Check(Rejects(() => TypedValues.Compile("x", outside)), "A member outside its struct is rejected");

        // Arrays: lower bounds, multiple dimensions, arrays of structs, stride and limits.
        var arr = new TypeNode { Kind = TypeKind.Array, Name = "ARRAY [1..2] OF INT", Size = 4, Stride = 2, Element = P("INT", 2) };
        arr.Dims.Add((1, 2));
        Check(TypedValues.Compile("MAIN.a", arr).Columns.Select(c => c.Name).SequenceEqual(new[] { "MAIN.a[1]", "MAIN.a[2]" }), "Real indices from lower bound 1");
        var grid = new TypeNode { Kind = TypeKind.Array, Name = "ARRAY [1..2, -1..0] OF BYTE", Size = 4, Stride = 1, Element = P("BYTE", 1) };
        grid.Dims.Add((1, 2)); grid.Dims.Add((-1, 2));
        var gp = TypedValues.Compile("MAIN.g", grid);
        Check(gp.Columns.Select(c => c.Name).SequenceEqual(new[] { "MAIN.g[1,-1]", "MAIN.g[1,0]", "MAIN.g[2,-1]", "MAIN.g[2,0]" }), "Rightmost index fastest, negative bounds kept");
        Check(string.Join(",", Cells(gp, new byte[] { 1, 2, 3, 4 }, out _, out _)) == "1,2,3,4", "Grid order matches storage order");
        var parts = new TypeNode { Kind = TypeKind.Array, Name = "ARRAY [0..1] OF ST_In", Size = 16, Stride = 8, Element = inner };
        parts.Dims.Add((0, 2));
        var pp = TypedValues.Compile("MAIN.p", parts);
        Check(pp.Columns.Select(c => c.Name + "@" + c.Offset).SequenceEqual(new[] { "MAIN.p[0].t@0", "MAIN.p[0].ok@4", "MAIN.p[1].t@8", "MAIN.p[1].ok@12" }), "Array of structs uses the padded stride");
        var wrong = new TypeNode { Kind = TypeKind.Array, Name = "ARRAY [0..2] OF INT", Size = 4, Stride = 2, Element = P("INT", 2) };
        wrong.Dims.Add((0, 3));
        Check(Rejects(() => TypedValues.Compile("x", wrong)), "Dimensions that do not fill the size are rejected");
        var many = new TypeNode { Kind = TypeKind.Array, Name = "ARRAY [0..256] OF BYTE", Size = 257, Stride = 1, Element = P("BYTE", 1) };
        many.Dims.Add((0, 257));
        Check(Rejects(() => TypedValues.Compile("x", many), "columns"), "More than 256 columns in a channel is rejected");
        var huge = new TypeNode { Kind = TypeKind.Array, Name = "huge", Size = int.MaxValue, Stride = 1, Element = P("BYTE", 1) };
        huge.Dims.Add((0, int.MaxValue));
        Check(Rejects(() => TypedValues.Compile("x", huge)), "A payload above 64 KiB is rejected before expansion");
        Check(Rejects(() => TypedValues.Compile("x", new TypeNode { Kind = TypeKind.Primitive, Name = "LDATE", Primitive = "LDATE", Size = 8 })), "LDATE is not decoded in the plan");

        // Raw storage: snapshot isolation, startup, bounds, payload length.
        var samples = new RecordingSamples(Enumerable.Repeat(8, 500).ToArray(), 500);
        Check(samples.MaxRows == 2000, $"500 numeric channels keep the v3 row limit, found {samples.MaxRows}");
        var one = new RecordingSamples(new[] { 1 }, 1);
        Check(one.MaxRows == 100000, "One numeric channel keeps the 100,000-row limit");
        var row = new byte[4000]; row[0] = 42;
        for (int i = 0; i < samples.MaxRows; i++) if (!samples.Add(DateTimeOffset.UtcNow, row)) throw new Exception("early recording limit");
        row[0] = 99;
        Check(!samples.Add(DateTimeOffset.UtcNow, row) && samples.LimitReached && samples.Rows.Count == 2000, "Rows are bounded under sustained input");
        Check(samples.Rows.TryDequeue(out var first) && first.Payload[0] == 42, "A retained row keeps its snapshot");
        var startup = new RecordingSamples(new[] { 2, 4 }, 2);
        startup.Update(0, new byte[] { 7, 0 }, DateTimeOffset.UtcNow);
        Check(startup.Rows.IsEmpty, "No row before every channel has a value");
        startup.Update(1, new byte[] { 8, 0, 0, 0 }, DateTimeOffset.UtcNow);
        var source = new byte[] { 9, 0 };
        startup.Update(0, source, DateTimeOffset.UtcNow);
        source[0] = 1;
        Check(startup.Rows.TryDequeue(out var r) && r.Payload.SequenceEqual(new byte[] { 9, 0, 8, 0, 0, 0 }), "Row starts after every channel and copies the notification");
        Check(Rejects(() => startup.Update(1, new byte[] { 1, 2 }, DateTimeOffset.UtcNow)), "A notification of the wrong length is rejected");
        var wide = new RecordingSamples(new[] { 64 * 1024 }, 256);
        Check(wide.MaxRows == (int)(RecordingSamples.ByteBudget / (64 * 1024 + 64)) && wide.MaxRows < 100000 / 256 + 1000, "Large payloads are bounded by the byte budget");
        Check(Rejects(() => new RecordingSamples(Enumerable.Repeat(64 * 1024, 17).ToArray(), 17)), "Latest payloads above 1 MiB are rejected");
        Check(RecordingSamples.CsvHeader("=SUM(A1)").StartsWith("\"'="), "CSV headers cannot introduce spreadsheet formulas");
        // Writes through the resolver: enums by declared name or value, the TIME family as unsigned integers.
        Check(TypedValues.EncodeWrite(e, "fault").SequenceEqual(BitConverter.GetBytes((short)-5)), "Enum write by declared name, any case");
        Check(TypedValues.EncodeWrite(e, "-5").SequenceEqual(BitConverter.GetBytes((short)-5)), "Enum write by declared value");
        Check(Rejects(() => TypedValues.EncodeWrite(e, "7")) && Rejects(() => TypedValues.EncodeWrite(e, "Nope")), "Undeclared enum values and names are refused");
        Check(TypedValues.EncodeWrite(e, "Running").SequenceEqual(BitConverter.GetBytes((short)10)), "A name sharing a value writes that value");
        Check(TypedValues.EncodeWrite(P("TIME", 4), "300").SequenceEqual(BitConverter.GetBytes(300u)), "TIME write in integer ms");
        Check(TypedValues.EncodeWrite(P("LTIME", 8), "18446744073709551615").SequenceEqual(BitConverter.GetBytes(ulong.MaxValue)), "LTIME write keeps 64 bits");
        Check(Rejects(() => TypedValues.EncodeWrite(P("TIME", 4), "T#300MS"), "IEC literals"), "IEC TIME literals stay rejected");
        Check(Rejects(() => TypedValues.EncodeWrite(P("TIME", 4), "-1")) && Rejects(() => TypedValues.EncodeWrite(P("TIME", 4), "4294967296")), "Negative or too large TIME values are refused");
        var sub = P("TIME", 4); sub.Subrange = true;
        Check(Rejects(() => TypedValues.EncodeWrite(sub, "5"), "subrange"), "A subrange write is refused");
        var longEnum = new TypeNode { Kind = TypeKind.Enum, Name = "E_Mode", Primitive = "INT", Size = 2, EnumValues = e.EnumValues };
        Check(Rejects(() => TypedValues.Compile(new string('p', 2045), longEnum), "longer than"), "The column-name limit includes the enum suffix");
        Check(TypedValues.EncodeWrite(P("REAL", 4), "1") == null && TypedValues.EncodeWrite(s, "x") == null, "Other kinds keep the existing write path");
        Check(TypedValues.Show(e, BitConverter.GetBytes((short)10)) == "10 (Run|Running)" && TypedValues.Show(P("TIME", 4), BitConverter.GetBytes(300u)) == "300", "Written values read back as text");
        Console.WriteLine("PASS: " + cases + " typed recording plan, decode and storage cases; no ADS or PLC used.");
    }
}
