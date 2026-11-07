using System;
using TwinCAT.TypeSystem;

namespace TcAutomation.Core
{
    /// <summary>
    /// Typed text for read_var and read_var_list. Enums and the TIME family are decoded from the
    /// bytes the read already returned; every other kind keeps the 3.0 output only.
    /// </summary>
    public static class AdsTypedRead
    {
        /// <summary>
        /// The type of an enum or primitive symbol, through any alias or subrange chain. Null for every
        /// other kind, before any member or element is resolved, and for any metadata failure.
        /// </summary>
        public static TypeNode Describe(object symbol)
        {
            try
            {
                var type = (symbol as ISymbol)?.DataType;
                return Scalar(type) ? AdsTypes.Describe(type, "value") : null;
            }
            catch (Exception) { return null; }  // unknown or rejected metadata keeps the 3.0 output
        }

        // IEnumType is also an IAliasType, so the enum test comes first.
        public static bool Scalar(IDataType type)
        {
            for (int links = 0; type != null && links <= 32; links++)
            {
                switch (type.Category)
                {
                    case DataTypeCategory.Enum:
                    case DataTypeCategory.Primitive:
                        return true;
                    case DataTypeCategory.Alias:
                        type = (type as IAliasType)?.BaseType; break;
                    case DataTypeCategory.SubRange:
                        type = (type as ISubRangeType)?.BaseType; break;
                    default:
                        return false;
                }
            }
            return false;
        }

        /// <summary>"0 (OFF)" for an enum, the integer in its unit for a TIME-family value, null otherwise.</summary>
        public static string Text(TypeNode node, byte[] raw, out string unit)
        {
            unit = null;
            if (node == null || raw == null || raw.Length != node.Size) return null;
            bool temporal = node.Kind == TypeKind.Primitive && TypedValues.TemporalSize(node.Primitive) == node.Size;
            if (node.Kind != TypeKind.Enum && !temporal) return null;
            string text;
            try { text = TypedValues.Show(node, raw); }
            catch (Exception) { return null; }
            if (temporal) unit = TypedValues.TemporalUnit(node.Primitive);
            return text;
        }
    }
}
