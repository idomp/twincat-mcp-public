using System;
using System.Globalization;
using System.Text.Json;
using TwinCAT.Ads;
namespace TcAutomation.Core
{
    public static class AdsValues
    {
        // A stopped PLC keeps its variables, so a read needs Run or Stop only.
        public static bool Readable(AdsState state) => state == AdsState.Run || state == AdsState.Stop;

        // A list write value: a JSON string as is, a number as its JSON text, a Boolean as TRUE or FALSE.
        public static string ScalarText(JsonElement value)
        {
            switch (value.ValueKind)
            {
                case JsonValueKind.String: return value.GetString();
                case JsonValueKind.Number: return value.GetRawText();
                case JsonValueKind.True: return "TRUE";
                case JsonValueKind.False: return "FALSE";
                default: throw new ArgumentException($"Values must be strings, numbers or Booleans, not {value.ValueKind}.");
            }
        }

        // Result text with an invariant decimal point, whatever the culture of this PC.
        // Infinity and NaN keep the 3.0 text of this PC, which is not a number format.
        public static string Text(object value)
        {
            if (value is float f && (float.IsInfinity(f) || float.IsNaN(f))) return f.ToString();
            if (value is double d && (double.IsInfinity(d) || double.IsNaN(d))) return d.ToString();
            return value is IFormattable v ? v.ToString(null, CultureInfo.InvariantCulture) : value?.ToString() ?? "null";
        }

        // .NET Framework parses "-0" and "-0.0" as positive zero. Keep the sign the caller wrote.
        public static float Real(string value)
        {
            float r = float.Parse(value, CultureInfo.InvariantCulture);
            return r == 0f && value.TrimStart().StartsWith("-", StringComparison.Ordinal) ? -0f : r;
        }
        public static double LReal(string value)
        {
            double r = double.Parse(value, CultureInfo.InvariantCulture);
            return r == 0d && value.TrimStart().StartsWith("-", StringComparison.Ordinal) ? -0d : r;
        }

        public static void ValidateSize(int size) { if (size < 1 || size > 1024 * 1024) throw new ArgumentException("ADS symbol size must be 1 byte to 1 MiB."); }
        public static bool Boolean(string value)
        {
            if (string.Equals(value, "TRUE", StringComparison.OrdinalIgnoreCase) || value == "1") return true;
            if (string.Equals(value, "FALSE", StringComparison.OrdinalIgnoreCase) || value == "0") return false;
            throw new ArgumentException("BOOL values must be TRUE, FALSE, 1 or 0.");
        }
        public static object Parse(string type, int size, string value)
        {
            ValidateSize(size);
            switch (type.ToUpperInvariant())
            {
                case "BOOL": return Boolean(value);
                case "BYTE": case "USINT": return byte.Parse(value, CultureInfo.InvariantCulture);
                case "SINT": return sbyte.Parse(value, CultureInfo.InvariantCulture);
                case "WORD": case "UINT": return ushort.Parse(value, CultureInfo.InvariantCulture);
                case "INT": return short.Parse(value, CultureInfo.InvariantCulture);
                case "DWORD": case "UDINT": return uint.Parse(value, CultureInfo.InvariantCulture);
                case "DINT": return int.Parse(value, CultureInfo.InvariantCulture);
                case "LWORD": case "ULINT": return ulong.Parse(value, CultureInfo.InvariantCulture);
                case "LINT": return long.Parse(value, CultureInfo.InvariantCulture);
                case "REAL": return Real(value);
                case "LREAL": return LReal(value);
                default:
                    if (type.StartsWith("STRING", StringComparison.OrdinalIgnoreCase))
                    { if (value == null || value.Length >= size) throw new ArgumentException("STRING value exceeds its symbol capacity."); return value; }
                    throw new ArgumentException("Unsupported ADS write type: " + type);
            }
        }
        public static int NumericSize(string type)
        {
            switch (type)
            {
                case "BOOL": case "BYTE": case "USINT": case "SINT": return 1;
                case "WORD": case "UINT": case "INT": return 2;
                case "DWORD": case "UDINT": case "DINT": case "REAL": return 4;
                case "LWORD": case "ULINT": case "LINT": case "LREAL": return 8;
                default: throw new ArgumentException("Recording supports numeric scalar types only: " + type);
            }
        }
    }
}
