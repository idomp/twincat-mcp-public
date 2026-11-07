using System;
using System.Collections.Generic;
using TcAutomation.Core;

static class TypedReadTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    static TypeNode Mode() => new TypeNode
    {
        Kind = TypeKind.Enum, Name = "E_SimMode", Primitive = "INT", Size = 2,
        EnumValues = new List<(string, byte[])> { ("OFF", new byte[] { 0, 0 }), ("MANUAL_IO", new byte[] { 1, 0 }), ("PLANT", new byte[] { 2, 0 }) },
    };
    static TypeNode P(string name, int size) => new TypeNode { Kind = TypeKind.Primitive, Name = name, Primitive = name, Size = size };

    public static void Run()
    {
        string unit;
        // The values measured on the simulator: enum "00 00" and TIME "E0 93 04 00".
        Check(AdsTypedRead.Text(Mode(), new byte[] { 0, 0 }, out unit) == "0 (OFF)" && unit == null, "An enum reads as value and name, with no unit");
        Check(AdsTypedRead.Text(Mode(), new byte[] { 2, 0 }, out unit) == "2 (PLANT)", "Another enum value reads by its name");
        Check(AdsTypedRead.Text(Mode(), new byte[] { 3, 0 }, out unit) == "3 ()", "An undeclared enum value keeps its number and no name");
        Check(AdsTypedRead.Text(P("TIME", 4), new byte[] { 0xE0, 0x93, 0x04, 0x00 }, out unit) == "300000" && unit == "ms", "TIME reads as ms");
        Check(AdsTypedRead.Text(P("LTIME", 8), BitConverter.GetBytes(5000000000UL), out unit) == "5000000000" && unit == "ns", "LTIME reads as ns");
        Check(AdsTypedRead.Text(P("TOD", 4), BitConverter.GetBytes(3600000u), out unit) == "3600000" && unit == "ms_since_midnight", "TOD reads as ms since midnight");
        Check(AdsTypedRead.Text(P("DT", 4), BitConverter.GetBytes(1u), out unit) == "1" && unit == "s_since_1970-01-01", "DT reads as seconds since 1970");

        Check(AdsTypedRead.Text(P("TIME_OF_DAY", 4), BitConverter.GetBytes(1u), out unit) == "1" && unit == "ms_since_midnight", "TIME_OF_DAY reads as ms since midnight");
        Check(AdsTypedRead.Text(P("DATE", 4), BitConverter.GetBytes(86400u), out unit) == "86400" && unit == "s_since_1970-01-01", "DATE reads as seconds since 1970");
        Check(AdsTypedRead.Text(P("DATE_AND_TIME", 4), BitConverter.GetBytes(2u), out unit) == "2" && unit == "s_since_1970-01-01", "DATE_AND_TIME reads as seconds since 1970");

        var twoNames = Mode(); twoNames.EnumValues.Add(("NONE", new byte[] { 0, 0 }));
        Check(AdsTypedRead.Text(twoNames, new byte[] { 0, 0 }, out unit) == "0 (OFF|NONE)", "Two names for one value are both given");
        var signed = new TypeNode { Kind = TypeKind.Enum, Name = "E_Err", Primitive = "DINT", Size = 4,
            EnumValues = new List<(string, byte[])> { ("ERR", BitConverter.GetBytes(-1)) } };
        Check(AdsTypedRead.Text(signed, BitConverter.GetBytes(-1), out unit) == "-1 (ERR)", "A negative enum value keeps its sign");
        var badRaw = new TypeNode { Kind = TypeKind.Enum, Name = "E_Bad", Primitive = "INT", Size = 2,
            EnumValues = new List<(string, byte[])> { ("X", new byte[] { 0 }) } };
        Check(AdsTypedRead.Text(badRaw, new byte[] { 0, 0 }, out unit) == null, "An enum with a wrong-size declared value gets no typed field");

        var alias = P("TIME", 4); alias.Name = "T_Timeout";
        Check(AdsTypedRead.Text(alias, BitConverter.GetBytes(250u), out unit) == "250" && unit == "ms", "An alias of TIME reads as TIME");

        Check(AdsTypedRead.Text(P("INT", 2), new byte[] { 1, 0 }, out unit) == null && unit == null, "A numeric primitive gets no typed field");
        var st = new TypeNode { Kind = TypeKind.Struct, Name = "ST_X", Size = 4 };
        st.Members.Add(("t", 0, P("TIME", 4)));
        Check(AdsTypedRead.Text(st, new byte[4], out unit) == null && unit == null, "A struct gets no typed field");
        Check(AdsTypedRead.Text(Mode(), new byte[] { 0 }, out unit) == null, "A size mismatch gets no typed field");
        Check(AdsTypedRead.Text(P("TIME", 8), new byte[8], out unit) == null && unit == null, "A TIME of the wrong size gets no typed field");
        Check(AdsTypedRead.Text(null, new byte[] { 0, 0 }, out unit) == null && AdsTypedRead.Text(Mode(), null, out unit) == null, "No metadata or no bytes: no typed field");
        Check(AdsTypedRead.Describe(null) == null, "No data type: no description, no exception");
        Console.WriteLine("PASS: " + cases + " typed read cases; no ADS or PLC used.");
    }
}
