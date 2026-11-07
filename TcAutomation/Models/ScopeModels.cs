using System.Collections.Generic;

namespace TcAutomation.Models
{
    public class ScopeCreateResult
    {
        public bool Success { get; set; }
        public string ConfigPath { get; set; } = "";
        public int ChannelCount { get; set; }
        public int SampleTimeMs { get; set; }
        public double? RecordTimeSec { get; set; }
        public List<string> Variables { get; set; } = new List<string>();
        public string? ErrorMessage { get; set; }
    }

    public class ScopeSessionResponse
    {
        public bool Success { get; set; }
        public string State { get; set; } = "";
        public string? ConfigPath { get; set; }
        public string? DataPath { get; set; }
        public string? Format { get; set; }
        public double ElapsedSeconds { get; set; }
        public int SamplesCollected { get; set; }
        public int ChannelCount { get; set; }
        public List<string> Columns { get; set; } = new List<string>();
        public string? StartedAt { get; set; }
        public long? FileSizeKB { get; set; }
        public string? ErrorMessage { get; set; }
    }

    public class ScopeExportResult
    {
        public bool Success { get; set; }
        public string InputPath { get; set; } = "";
        public string OutputPath { get; set; } = "";
        public string Format { get; set; } = "";
        public long FileSizeKB { get; set; }
        public string? ErrorMessage { get; set; }
    }

    public class AdsRecordResult
    {
        public bool LimitReached { get; set; }
        public bool Success { get; set; }
        public string OutputPath { get; set; } = "";
        public int ChannelCount { get; set; }
        public int SamplesCollected { get; set; }
        public double DurationSeconds { get; set; }
        public int SampleTimeMs { get; set; }
        public List<string> Variables { get; set; } = new List<string>();
        public long FileSizeKB { get; set; }
        public string? ErrorMessage { get; set; }
        public string? StartTrigger { get; set; }
        public string? StopTrigger { get; set; }
        public string? TriggerStatus { get; set; }
        public string FormatVersion { get; set; } = "typed-csv-v1";
        public string RowEmission { get; set; } = "channel0";
        public int ColumnCount { get; set; }
        public List<RecordColumn> Columns { get; set; } = new List<RecordColumn>();
        public int MaxRows { get; set; }
        public int DecodeErrorCount { get; set; }
        public List<string> DecodeErrors { get; set; } = new List<string>();
        public bool DecodeErrorsTruncated { get; set; }
        public int UnknownEnumValueCount { get; set; }
    }

    /// <summary>One CSV data column, in CSV order.</summary>
    public class RecordColumn
    {
        public int Index { get; set; }
        public string Name { get; set; } = "";
        public int Channel { get; set; }
        public string DeclaredType { get; set; } = "";
        public string Kind { get; set; } = "";
        public string? Unit { get; set; }
        public int Offset { get; set; }
        public int Size { get; set; }
        public int? CodePage { get; set; }
    }
}
