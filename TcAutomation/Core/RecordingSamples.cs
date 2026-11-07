using System;
using System.Collections.Concurrent;
using System.Linq;
namespace TcAutomation.Core
{
    /// <summary>
    /// Latest raw payload per channel, and rows of copied payloads emitted on channel 0 once every
    /// channel has a value. Decoding happens at export, so the notification callback only copies bytes.
    /// </summary>
    public sealed class RecordingSamples
    {
        public const int MaxChannels = 500, MaxColumns = 4096, MaxCells = 1000000, MaxRowLimit = 100000;
        public const int MaxLatestBytes = 1024 * 1024;
        public const long ByteBudget = 64L * 1024 * 1024;
        const int RowOverhead = 64;  // charged per retained row for the array and queue entry

        readonly int channels;
        int count, receivedCount;
        readonly byte[] latest;
        readonly bool[] received;
        readonly object gate = new object();
        public int[] Sizes { get; }
        public int[] Starts { get; }
        public int RowBytes { get; }
        public int MaxRows { get; }
        public bool LimitReached { get; private set; }
        public ConcurrentQueue<(DateTimeOffset Timestamp, byte[] Payload)> Rows { get; } = new ConcurrentQueue<(DateTimeOffset, byte[])>();

        public RecordingSamples(int[] sizes, int columns)
        {
            if (sizes == null || sizes.Length < 1 || sizes.Length > MaxChannels) throw new ArgumentException($"Expected 1..{MaxChannels} channels.");
            if (sizes.Any(s => s < 1 || s > TypedValues.MaxPayload)) throw new ArgumentException($"Each channel payload must be 1..{TypedValues.MaxPayload} bytes.");
            if (columns < sizes.Length || columns > MaxColumns) throw new ArgumentException($"Expected {sizes.Length}..{MaxColumns} columns.");
            long total = sizes.Sum(s => (long)s);
            if (total > MaxLatestBytes) throw new ArgumentException($"Channel payloads total {total} bytes, above {MaxLatestBytes}.");
            channels = sizes.Length; Sizes = (int[])sizes.Clone(); RowBytes = (int)total;
            Starts = new int[channels];
            for (int i = 1; i < channels; i++) Starts[i] = Starts[i - 1] + sizes[i - 1];
            latest = new byte[RowBytes]; received = new bool[channels];
            // For numeric channels (at most 8 bytes per column) the byte budget never binds before the cell limit.
            MaxRows = (int)Math.Min(Math.Min(MaxRowLimit, MaxCells / columns), ByteBudget / (RowBytes + RowOverhead));
            if (MaxRows < 1) throw new ArgumentException("No row fits the recording limits.");
        }

        public static string CsvHeader(string name) => TypedValues.Text(name);

        /// <summary>Copies one notification. A payload of the wrong length throws, so no row holds misaligned data.</summary>
        public bool Update(int index, byte[] data, DateTimeOffset timestamp)
        {
            if (data == null || data.Length != Sizes[index]) throw new ArgumentException($"Channel {index} notification is {data?.Length} bytes, expected {Sizes[index]}.");
            lock (gate)
            {
                Buffer.BlockCopy(data, 0, latest, Starts[index], data.Length);
                if (!received[index]) { received[index] = true; receivedCount++; }
                return index != 0 || receivedCount != channels || Add(timestamp, latest);
            }
        }

        public bool Add(DateTimeOffset timestamp, byte[] payload)
        {
            if (payload == null || payload.Length != RowBytes) throw new ArgumentException("Row payload size mismatch.");
            lock (gate)
            {
                if (count >= MaxRows) { LimitReached = true; return false; }
                Rows.Enqueue((timestamp, (byte[])payload.Clone())); count++; return true;
            }
        }
    }
}
