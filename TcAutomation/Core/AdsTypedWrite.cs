using System;
using TwinCAT.Ads;
using TwinCAT.TypeSystem;

namespace TcAutomation.Core
{
    /// <summary>Writes through the shared type resolver, so aliases, enums and the TIME family need no name guess.</summary>
    public static class AdsTypedWrite
    {
        /// <summary>The numeric primitive behind an alias or subrange, or the fallback name.</summary>
        public static string NumericName(IDataType type, string fallback)
        {
            try
            {
                var node = AdsTypes.Describe(type, "value");
                // A subrange keeps its declared name, so the existing path refuses it instead of writing past its bounds.
                return node.Kind == TypeKind.Primitive && TypedValues.IsNumeric(node.Primitive) && !node.Subrange ? node.Primitive : fallback;
            }
            catch (Exception) { return fallback; }  // unknown metadata keeps the 3.0 behaviour
        }

        /// <summary>
        /// Writes an enum or TIME-family value and reads it back. Returns false, with nothing read or
        /// written, for any other kind. beforeWrite runs just before the write is sent.
        /// </summary>
        public static bool TryWrite(AdsClient client, uint handle, IDataType type, int size, string value, Action beforeWrite,
                                    out string previous, out string written)
        {
            previous = written = null;
            TypeNode node;
            try { node = AdsTypes.Describe(type, "value"); }
            catch (Exception) { return false; }  // unknown metadata keeps the 3.0 path
            var bytes = TypedValues.EncodeWrite(node, value);  // throws on invalid input, before any ADS call
            if (bytes == null) return false;
            if (bytes.Length != size || node.Size != size) throw new ArgumentException($"{node.Name} is {size} bytes, the value {bytes.Length}.");
            var before = new byte[size];
            client.Read(handle, before.AsMemory());
            previous = TypedValues.Show(node, before);
            beforeWrite();
            client.Write(handle, bytes.AsMemory());
            var after = new byte[size];
            client.Read(handle, after.AsMemory());
            written = TypedValues.Show(node, after);
            return true;
        }
    }
}
