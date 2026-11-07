using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;

namespace TcAutomation.Core
{
    public enum TypeKind { Primitive, Enum, String, Struct, Array }

    /// <summary>
    /// An application-owned type description, built from ADS metadata once before subscribing.
    /// The decoder reads only this description, never the SDK, so it is testable without a PLC.
    /// </summary>
    public sealed class TypeNode
    {
        public TypeKind Kind;
        public string Name = "";                 // declared type name, for metadata
        public int Size;                         // byte size of the whole value
        public bool Subrange;                    // reached through a subrange: its bounds are not enforced here
        public string Primitive = "";            // Primitive: exact IEC name; Enum: the base primitive
        public List<(string Name, byte[] Raw)> EnumValues = new List<(string, byte[])>();
        public int CodePage;                     // String: 1200 for WSTRING
        public List<(string Name, int Offset, TypeNode Type)> Members = new List<(string, int, TypeNode)>();
        public List<(int Lower, int Count)> Dims = new List<(int, int)>();
        public int Stride;                       // Array: byte size of one element, padding included
        public TypeNode Element;
    }

    public enum LeafKind { Numeric, EnumValue, EnumName, Duration, Text }

    public sealed class Column
    {
        public string Name = "";
        public int Channel;
        public int Offset;                        // within the channel payload
        public int Size;
        public LeafKind Kind;
        public string Primitive = "";
        public string DeclaredType = "";
        public string Unit = "";
        public int CodePage;
        public TypeNode Enum;
    }

    public sealed class ChannelPlan
    {
        public string Path = "";
        public int Size;
        public TypeNode Type;
        public List<Column> Columns = new List<Column>();
    }

    /// <summary>Compiles a description into columns and decodes channel payloads into CSV cells.</summary>
    public static class TypedValues
    {
        public const int MaxDepth = 32, MaxRank = 8, MaxColumnsPerChannel = 256, MaxNameLength = 2048, MaxPayload = 64 * 1024;

        static readonly Dictionary<string, int> Numeric = new Dictionary<string, int>
        {
            ["BOOL"] = 1, ["BYTE"] = 1, ["USINT"] = 1, ["SINT"] = 1, ["WORD"] = 2, ["UINT"] = 2, ["INT"] = 2,
            ["DWORD"] = 4, ["UDINT"] = 4, ["DINT"] = 4, ["REAL"] = 4, ["LWORD"] = 8, ["ULINT"] = 8, ["LINT"] = 8, ["LREAL"] = 8,
        };
        // Beckhoff: TIME and TOD are UDINT milliseconds, LTIME is ULINT nanoseconds, DATE and DT are UDINT seconds since 1970.
        static readonly Dictionary<string, (int Size, string Unit)> Temporal = new Dictionary<string, (int, string)>
        {
            ["TIME"] = (4, "ms"), ["TOD"] = (4, "ms_since_midnight"), ["TIME_OF_DAY"] = (4, "ms_since_midnight"),
            ["LTIME"] = (8, "ns"), ["DATE"] = (4, "s_since_1970-01-01"), ["DT"] = (4, "s_since_1970-01-01"), ["DATE_AND_TIME"] = (4, "s_since_1970-01-01"),
        };
        static readonly HashSet<string> IntegerBases = new HashSet<string> { "BYTE", "USINT", "SINT", "WORD", "UINT", "INT", "DWORD", "UDINT", "DINT", "LWORD", "ULINT", "LINT" };

        public static bool IsNumeric(string primitive) => Numeric.ContainsKey(primitive);
        public static int NumericSize(string primitive) => Numeric.TryGetValue(primitive, out var n) ? n : 0;
        public static bool IsTemporal(string primitive) => Temporal.ContainsKey(primitive);
        public static int TemporalSize(string primitive) => Temporal.TryGetValue(primitive, out var t) ? t.Size : 0;
        public static string TemporalUnit(string primitive) => Temporal.TryGetValue(primitive, out var t) ? t.Unit : null;
        public static bool IsIntegerBase(string primitive) => IntegerBases.Contains(primitive);

        public static ChannelPlan Compile(string path, TypeNode type, int channel = 0)
        {
            if (type == null) throw new ArgumentException($"{path}: no type description.");
            if (type.Size < 1 || type.Size > MaxPayload) throw new ArgumentException($"{path}: {type.Size} bytes is outside 1..{MaxPayload}.");
            var plan = new ChannelPlan { Path = path, Size = type.Size, Type = type };
            Expand(plan, path, type, 0, 0, channel);
            if (plan.Columns.Select(c => c.Name).Distinct(StringComparer.Ordinal).Count() != plan.Columns.Count)
                throw new ArgumentException($"{path}: duplicate column names.");
            return plan;
        }

        static void Expand(ChannelPlan plan, string path, TypeNode t, int offset, int depth, int channel)
        {
            if (depth > MaxDepth) throw new ArgumentException($"{path}: nesting deeper than {MaxDepth}.");
            if (path.Length > MaxNameLength) throw new ArgumentException($"{path.Substring(0, 64)}...: column name longer than {MaxNameLength}.");
            if (offset < 0 || t.Size < 1 || (long)offset + t.Size > plan.Size) throw new ArgumentException($"{path}: storage outside its container.");
            void Add(LeafKind kind, string name, string unit = "")
            {
                if (name.Length > MaxNameLength) throw new ArgumentException($"{name.Substring(0, 64)}...: column name longer than {MaxNameLength}.");
                if (plan.Columns.Count >= MaxColumnsPerChannel) throw new ArgumentException($"{plan.Path}: more than {MaxColumnsPerChannel} columns.");
                plan.Columns.Add(new Column { Name = name, Channel = channel, Offset = offset, Size = t.Size, Kind = kind, Primitive = t.Primitive,
                                              DeclaredType = t.Name, Unit = unit, CodePage = t.CodePage, Enum = t.Kind == TypeKind.Enum ? t : null });
            }
            switch (t.Kind)
            {
                case TypeKind.Primitive:
                    if (Numeric.TryGetValue(t.Primitive, out var n) && n == t.Size) Add(LeafKind.Numeric, path);
                    else if (Temporal.TryGetValue(t.Primitive, out var tm) && tm.Size == t.Size) Add(LeafKind.Duration, path, tm.Unit);
                    else throw new ArgumentException($"{path}: unsupported primitive {t.Primitive} of {t.Size} bytes.");
                    break;
                case TypeKind.Enum:
                    if (!IntegerBases.Contains(t.Primitive) || Numeric[t.Primitive] != t.Size || t.EnumValues.Any(v => v.Raw == null || v.Raw.Length != t.Size))
                        throw new ArgumentException($"{path}: enum {t.Name} has no consistent integer base.");
                    Add(LeafKind.EnumValue, path + ".value");
                    Add(LeafKind.EnumName, path + ".name");
                    break;
                case TypeKind.String:
                    if (t.CodePage == 1200 ? t.Size < 2 || t.Size % 2 != 0 : t.CodePage <= 0)
                        throw new ArgumentException($"{path}: string {t.Name} has no usable encoding or size.");
                    Add(LeafKind.Text, path);
                    break;
                case TypeKind.Struct:
                    if (t.Members.Count == 0) throw new ArgumentException($"{path}: struct {t.Name} has no members.");
                    int end = 0;
                    foreach (var m in t.Members.OrderBy(m => m.Offset).ThenBy(m => m.Name, StringComparer.Ordinal))
                    {
                        if (m.Offset < end) throw new ArgumentException($"{path}.{m.Name}: overlaps the previous member.");
                        if ((long)m.Offset + m.Type.Size > t.Size) throw new ArgumentException($"{path}.{m.Name}: outside struct {t.Name}.");
                        Expand(plan, path + "." + m.Name, m.Type, offset + m.Offset, depth + 1, channel);
                        end = m.Offset + m.Type.Size;
                    }
                    break;
                case TypeKind.Array:
                    if (t.Dims.Count < 1 || t.Dims.Count > MaxRank) throw new ArgumentException($"{path}: rank {t.Dims.Count} is outside 1..{MaxRank}.");
                    if (t.Element == null || t.Stride < t.Element.Size || t.Dims.Any(d => d.Count < 1)) throw new ArgumentException($"{path}: inconsistent array layout.");
                    long count = 1;
                    foreach (var d in t.Dims) { count = checked(count * d.Count); if ((long)d.Lower + d.Count - 1 > int.MaxValue) throw new ArgumentException($"{path}: bound overflow."); }
                    if (checked(count * t.Stride) != t.Size) throw new ArgumentException($"{path}: {count} elements of {t.Stride} bytes do not fill {t.Size} bytes.");
                    if (count > MaxColumnsPerChannel) throw new ArgumentException($"{plan.Path}: more than {MaxColumnsPerChannel} columns.");
                    var index = new int[t.Dims.Count];
                    for (long linear = 0; linear < count; linear++)
                    {
                        long rest = linear;  // rightmost index varies fastest
                        for (int d = t.Dims.Count - 1; d >= 0; d--) { index[d] = t.Dims[d].Lower + (int)(rest % t.Dims[d].Count); rest /= t.Dims[d].Count; }
                        Expand(plan, path + "[" + string.Join(",", index) + "]", t.Element, checked(offset + (int)(linear * t.Stride)), depth + 1, channel);
                    }
                    break;
                default:
                    throw new ArgumentException($"{path}: unsupported type {t.Name}.");
            }
        }

        public static string Text(string value)
        {
            if (value.Length > 0 && "=+-@\t\r\n".IndexOf(value[0]) >= 0) value = "'" + value;
            return "\"" + value.Replace("\"", "\"\"") + "\"";
        }

        /// <summary>One CSV cell. A value that cannot be decoded gives a marker and an error code, never a guess.</summary>
        public static string Cell(Column c, byte[] payload, int start, out string error, out bool unknownEnum)
        {
            error = null; unknownEnum = false;
            int at = start + c.Offset;
            switch (c.Kind)
            {
                case LeafKind.Numeric:
                    return NumericValue(c.Primitive, payload, at).ToString("G", CultureInfo.InvariantCulture);
                case LeafKind.Duration:
                    return c.Size == 8 ? BitConverter.ToUInt64(payload, at).ToString(CultureInfo.InvariantCulture)
                                       : BitConverter.ToUInt32(payload, at).ToString(CultureInfo.InvariantCulture);
                case LeafKind.EnumValue:
                    return IntegerText(c.Primitive, payload, at);
                case LeafKind.EnumName:
                    var names = c.Enum.EnumValues.Where(v => Same(v.Raw, payload, at)).Select(v => v.Name).ToList();
                    unknownEnum = names.Count == 0;
                    return Text(string.Join("|", names));
                case LeafKind.Text:
                    var text = DecodeString(payload, at, c.Size, c.CodePage, out error);
                    return error == null ? Text(text) : Text("#DECODE_ERROR:" + error);
                default:
                    throw new InvalidOperationException("Unknown column kind.");
            }
        }

        static bool Same(byte[] raw, byte[] payload, int at)
        {
            for (int i = 0; i < raw.Length; i++) if (payload[at + i] != raw[i]) return false;
            return true;
        }

        // The v3 conversion, so numeric output stays byte-identical, including rounding of 64-bit integers above 2^53.
        public static double NumericValue(string primitive, byte[] b, int at)
        {
            switch (primitive)
            {
                case "LREAL": return BitConverter.ToDouble(b, at);
                case "REAL": return BitConverter.ToSingle(b, at);
                case "LINT": return BitConverter.ToInt64(b, at);
                case "DINT": return BitConverter.ToInt32(b, at);
                case "INT": return BitConverter.ToInt16(b, at);
                case "SINT": return (sbyte)b[at];
                case "ULINT": case "LWORD": return BitConverter.ToUInt64(b, at);
                case "UDINT": case "DWORD": return BitConverter.ToUInt32(b, at);
                case "UINT": case "WORD": return BitConverter.ToUInt16(b, at);
                case "USINT": case "BYTE": return b[at];
                case "BOOL": return b[at] != 0 ? 1.0 : 0.0;
                default: throw new ArgumentException("Unsupported numeric type " + primitive);
            }
        }

        static string IntegerText(string primitive, byte[] b, int at)
        {
            switch (primitive)
            {
                case "LINT": return BitConverter.ToInt64(b, at).ToString(CultureInfo.InvariantCulture);
                case "DINT": return BitConverter.ToInt32(b, at).ToString(CultureInfo.InvariantCulture);
                case "INT": return BitConverter.ToInt16(b, at).ToString(CultureInfo.InvariantCulture);
                case "SINT": return ((sbyte)b[at]).ToString(CultureInfo.InvariantCulture);
                case "ULINT": case "LWORD": return BitConverter.ToUInt64(b, at).ToString(CultureInfo.InvariantCulture);
                case "UDINT": case "DWORD": return BitConverter.ToUInt32(b, at).ToString(CultureInfo.InvariantCulture);
                case "UINT": case "WORD": return BitConverter.ToUInt16(b, at).ToString(CultureInfo.InvariantCulture);
                case "USINT": case "BYTE": return b[at].ToString(CultureInfo.InvariantCulture);
                default: throw new ArgumentException("Unsupported enum base " + primitive);
            }
        }

        /// <summary>
        /// The bytes to write for an enum or a TIME-family value, or null for any other kind. An enum takes
        /// a declared name or the integer of a declared value. TIME, TOD, DATE, DT and LTIME take an unsigned
        /// integer in their recording unit; IEC literals such as T#5S are not accepted. Throws before any write.
        /// </summary>
        public static byte[] EncodeWrite(TypeNode t, string value)
        {
            string text = (value ?? "").Trim();
            if (t.Subrange) throw new ArgumentException($"{t.Name} is a subrange; writes to subranges are not supported.");
            if (t.Kind == TypeKind.Primitive && Temporal.TryGetValue(t.Primitive, out var tm) && tm.Size == t.Size)
            {
                // NumberStyles.None: digits only, so a sign, a space or an IEC literal fails.
                if (t.Size == 4 && uint.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out var u4)) return BitConverter.GetBytes(u4);
                if (t.Size == 8 && ulong.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out var u8)) return BitConverter.GetBytes(u8);
                throw new ArgumentException($"{t.Primitive} values are unsigned integers in {tm.Unit}; IEC literals are not accepted.");
            }
            if (t.Kind != TypeKind.Enum) return null;
            var byName = t.EnumValues.Where(v => string.Equals(v.Name, text, StringComparison.OrdinalIgnoreCase)).ToList();
            if (byName.Count == 1) return (byte[])byName[0].Raw.Clone();
            var raw = IntegerBytes(t.Primitive, text);
            if (raw == null || !t.EnumValues.Any(v => v.Raw.SequenceEqual(raw)))
                throw new ArgumentException($"'{value}' is not a declared value of {t.Name}.");
            return raw;
        }

        static byte[] IntegerBytes(string primitive, string s)
        {
            var signed = NumberStyles.AllowLeadingSign;
            var c = CultureInfo.InvariantCulture;
            switch (primitive)
            {
                case "SINT": return sbyte.TryParse(s, signed, c, out var a) ? new[] { (byte)a } : null;
                case "USINT": case "BYTE": return byte.TryParse(s, NumberStyles.None, c, out var b) ? new[] { b } : null;
                case "INT": return short.TryParse(s, signed, c, out var d) ? BitConverter.GetBytes(d) : null;
                case "UINT": case "WORD": return ushort.TryParse(s, NumberStyles.None, c, out var e) ? BitConverter.GetBytes(e) : null;
                case "DINT": return int.TryParse(s, signed, c, out var f) ? BitConverter.GetBytes(f) : null;
                case "UDINT": case "DWORD": return uint.TryParse(s, NumberStyles.None, c, out var g) ? BitConverter.GetBytes(g) : null;
                case "LINT": return long.TryParse(s, signed, c, out var h) ? BitConverter.GetBytes(h) : null;
                case "ULINT": case "LWORD": return ulong.TryParse(s, NumberStyles.None, c, out var i) ? BitConverter.GetBytes(i) : null;
                default: return null;
            }
        }

        /// <summary>A written or read value as text: an enum as "value (names)", a TIME-family value as its integer.</summary>
        public static string Show(TypeNode t, byte[] b)
        {
            var cells = Compile("v", t).Columns.Select(col => Cell(col, b, 0, out _, out _)).ToList();
            return t.Kind == TypeKind.Enum ? $"{cells[0]} ({cells[1].Trim('"')})" : cells[0];
        }

        public static string DecodeString(byte[] b, int at, int size, int codePage, out string error)
        {
            error = null;
            int unit = codePage == 1200 ? 2 : 1, length = -1;
            for (int i = 0; i + unit <= size; i += unit)
                if (b[at + i] == 0 && (unit == 1 || b[at + i + 1] == 0)) { length = i; break; }
            if (length < 0) { error = "unterminated_string"; return null; }
            try
            {
                var encoding = codePage == 1200 ? new UnicodeEncoding(false, false, true)
                    : codePage == 65001 ? new UTF8Encoding(false, true)
                    : Encoding.GetEncoding(codePage, EncoderFallback.ExceptionFallback, DecoderFallback.ExceptionFallback);
                return encoding.GetString(b, at, length);
            }
            catch (DecoderFallbackException) { error = "invalid_encoding"; return null; }
            catch (ArgumentException) { error = "invalid_encoding"; return null; }
        }
    }
}
