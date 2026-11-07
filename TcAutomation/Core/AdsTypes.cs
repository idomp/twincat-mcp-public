using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text;
using TwinCAT.TypeSystem;

namespace TcAutomation.Core
{
    /// <summary>
    /// Builds a TypeNode from ADS metadata. Dispatches on the category first, never on a name prefix
    /// or a byte size. A primitive is identified by its exact IEC name, as AdsValues already does.
    /// </summary>
    public static class AdsTypes
    {
        const int MaxLinks = 32, MaxNodes = 16384;

        sealed class Identity : IEqualityComparer<IDataType>
        {
            public bool Equals(IDataType a, IDataType b) => ReferenceEquals(a, b);
            public int GetHashCode(IDataType t) => RuntimeHelpers.GetHashCode(t);
        }

        public static TypeNode Describe(IDataType type, string path, Encoding instanceEncoding = null)
        {
            int nodes = 0;
            return Describe(type, path, instanceEncoding, 0, new HashSet<IDataType>(new Identity()), ref nodes);
        }

        static TypeNode Describe(IDataType t, string path, Encoding encoding, int depth, HashSet<IDataType> active, ref int nodes)
        {
            if (t == null) throw new ArgumentException($"{path}: unresolved type.");
            if (depth > TypedValues.MaxDepth) throw new ArgumentException($"{path}: nesting deeper than {TypedValues.MaxDepth}.");
            if (++nodes > MaxNodes) throw new ArgumentException($"{path}: more than {MaxNodes} type nodes.");
            if (!active.Add(t)) throw new ArgumentException($"{path}: type {t.Name} contains itself.");
            try
            {
                int size = ((IBitSize)t).ByteSize;
                if (((IBitSize)t).IsBitType) throw new ArgumentException($"{path}: BIT types are not supported.");
                switch (t.Category)
                {
                    case DataTypeCategory.Alias:
                    case DataTypeCategory.SubRange:
                    {
                        var current = t;
                        bool subrange = false;
                        for (int links = 0; current.Category == DataTypeCategory.Alias || current.Category == DataTypeCategory.SubRange; links++)
                        {
                            if (links >= MaxLinks) throw new ArgumentException($"{path}: more than {MaxLinks} alias links.");
                            if (((IBitSize)current).ByteSize != size) throw new ArgumentException($"{path}: {current.Name} in the alias chain of {t.Name} has another size.");
                            subrange |= current.Category == DataTypeCategory.SubRange;
                            current = current.Category == DataTypeCategory.Alias ? ((IAliasType)current).BaseType : ((ISubRangeType)current).BaseType;
                            if (current == null) throw new ArgumentException($"{path}: {t.Name} has no resolved base type.");
                        }
                        var inner = Describe(current, path, encoding, depth + 1, active, ref nodes);
                        if (inner.Size != size) throw new ArgumentException($"{path}: {t.Name} is {size} bytes, its base {inner.Size}.");
                        inner.Name = t.Name;
                        inner.Subrange = subrange;
                        return inner;
                    }
                    case DataTypeCategory.Primitive:
                    {
                        string name = (t.Name ?? "").ToUpperInvariant();
                        bool known = TypedValues.NumericSize(name) == size || TypedValues.TemporalSize(name) == size;
                        if (!known)
                            throw new ArgumentException(name == "LDATE" || name == "LDT" || name == "LTOD" || name == "LDATE_AND_TIME" || name == "LTIME_OF_DAY"
                                ? $"{path}: {name} is not qualified yet (needs a TwinCAT 3.1.4026 live test)."
                                : $"{path}: unsupported primitive {t.Name} of {size} bytes.");
                        return new TypeNode { Kind = TypeKind.Primitive, Name = t.Name, Primitive = name, Size = size };
                    }
                    case DataTypeCategory.Enum:
                    {
                        var baseType = ((IAliasType)t).BaseType;
                        var b = Describe(baseType, path, null, depth + 1, active, ref nodes);
                        if (b.Kind != TypeKind.Primitive || !TypedValues.IsIntegerBase(b.Primitive) || b.Size != size)
                            throw new ArgumentException($"{path}: enum {t.Name} has no integer base of {size} bytes.");
                        var node = new TypeNode { Kind = TypeKind.Enum, Name = t.Name, Primitive = b.Primitive, Size = size };
                        foreach (IEnumValue v in ((IEnumType)t).EnumValues)
                        {
                            if (++nodes > MaxNodes) throw new ArgumentException($"{path}: more than {MaxNodes} type nodes and enum values.");
                            var raw = v.RawValue;
                            if (raw == null || raw.Length != size) throw new ArgumentException($"{path}: enum {t.Name} value {v.Name} is not {size} bytes.");
                            node.EnumValues.Add((v.Name, (byte[])raw.Clone()));
                        }
                        return node;
                    }
                    case DataTypeCategory.String:
                    {
                        var s = (IStringType)t;
                        if (!s.IsFixedLength) throw new ArgumentException($"{path}: variable-length strings are not supported.");
                        var e = encoding ?? s.Encoding;
                        if (e == null) throw new ArgumentException($"{path}: string {t.Name} has no encoding.");
                        return new TypeNode { Kind = TypeKind.String, Name = t.Name, Size = size, CodePage = e.CodePage };
                    }
                    case DataTypeCategory.Struct:
                    {
                        var node = new TypeNode { Kind = TypeKind.Struct, Name = t.Name, Size = size };
                        foreach (IMember m in ((IInterfaceType)t).AllMembers)  // AllMembers includes inherited members
                        {
                            string member = path + "." + m.InstanceName;
                            if (m.IsStatic || m.IsProperty) throw new ArgumentException($"{member}: static members and properties are not supported.");
                            if (((IBitSize)m).IsBitType) throw new ArgumentException($"{member}: BIT members are not supported.");
                            var memberEncoding = (m as IAttributedInstance)?.ValueEncoding;
                            node.Members.Add((m.InstanceName, m.ByteOffset, Describe(m.DataType, member, memberEncoding, depth + 1, active, ref nodes)));
                        }
                        return node;
                    }
                    case DataTypeCategory.Array:
                    {
                        var a = (IArrayType)t;
                        if (a.IsJagged) throw new ArgumentException($"{path}: jagged arrays are not supported.");
                        if (!(t is TwinCAT.Ads.TypeSystem.ArrayType concrete)) throw new ArgumentException($"{path}: no element stride in the metadata.");
                        if (concrete.IsOversampled) throw new ArgumentException($"{path}: oversampled arrays are not supported.");
                        var node = new TypeNode { Kind = TypeKind.Array, Name = t.Name, Size = size, Stride = concrete.ElementSize };
                        foreach (IDimension d in a.Dimensions) node.Dims.Add((d.LowerBound, d.ElementCount));
                        node.Element = Describe(a.ElementType, path + "[]", encoding, depth + 1, active, ref nodes);
                        return node;
                    }
                    default:
                        throw new ArgumentException($"{path}: {t.Category} {t.Name} is not supported.");
                }
            }
            finally { active.Remove(t); }
        }
    }
}
