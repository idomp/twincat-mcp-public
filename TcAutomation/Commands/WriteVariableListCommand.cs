using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text.Json;
using TwinCAT.Ads;

namespace TcAutomation.Commands
{
    public static class WriteVariableListCommand
    {
        public static int Execute(string amsNetId, int port, string variablesJson)
        {
            var result = new WriteVariableListResult
            {
                AmsNetId = amsNetId,
                Port = port
            };

            // Parse input JSON. Accept any JSON value kind per symbol (string,
            // number, bool) and normalize to a string — agents routinely send
            // {"x": 42} or {"b": true} rather than quoted strings, and a strict
            // Dictionary<string,string> would throw and fail the whole batch.
            Dictionary<string, JsonElement> rawVariables;
            try
            {
                rawVariables = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(variablesJson);
            }
            catch (Exception ex)
            {
                result.ErrorMessage = $"Failed to parse variablesJson: {ex.Message}";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 1;
            }

            if (rawVariables == null || rawVariables.Count == 0)
            {
                result.ErrorMessage = "No variables specified";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 1;
            }

            if (rawVariables.Count > 500)
            {
                result.ErrorMessage = "Maximum 500 variables per batch";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 1;
            }

            var variables = new Dictionary<string, string>(rawVariables.Count);
            foreach (var kvp in rawVariables)
            {
                variables[kvp.Key] = kvp.Value.ValueKind == JsonValueKind.String
                    ? (kvp.Value.GetString() ?? "")
                    : kvp.Value.ToString();
            }

            result.SymbolCount = variables.Count;

            try
            {
                using (var adsClient = new AdsClient())
                {
                    adsClient.Connect(amsNetId, port);

                    if (!adsClient.IsConnected)
                    {
                        result.ErrorMessage = $"Failed to connect to {amsNetId}:{port}";
                        Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                        return 1;
                    }

                    var stateInfo = adsClient.ReadState();
                    if (stateInfo.AdsState != AdsState.Run)
                    {
                        result.ErrorMessage = $"PLC is not running (state: {stateInfo.AdsState}). Cannot write variables.";
                        Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                        return 1;
                    }

                    foreach (var kvp in variables)
                    {
                        string symbolName = kvp.Key;
                        string value = kvp.Value;
                        var itemResult = new WriteVariableItemResult { Symbol = symbolName };
                        uint handle = 0;
                        bool handleCreated = false;

                        try
                        {
                            var symbolInfo = adsClient.ReadSymbol(symbolName);
                            itemResult.DataType = symbolInfo.TypeName;

                            handle = adsClient.CreateVariableHandle(symbolName);
                            handleCreated = true;

                            object previousValue = ReadTypedValue(adsClient, handle, symbolInfo.TypeName, symbolInfo.Size);
                            itemResult.PreviousValue = previousValue != null
                                ? Convert.ToString(previousValue, CultureInfo.InvariantCulture)
                                : "null";

                            WriteTypedValue(adsClient, handle, symbolInfo.TypeName, symbolInfo.Size, value);

                            object newValue = ReadTypedValue(adsClient, handle, symbolInfo.TypeName, symbolInfo.Size);
                            itemResult.NewValue = newValue != null
                                ? Convert.ToString(newValue, CultureInfo.InvariantCulture)
                                : "null";

                            itemResult.Success = true;
                            result.SuccessCount++;
                        }
                        catch (AdsErrorException ex)
                        {
                            itemResult.ErrorMessage = $"ADS Error: {ex.ErrorCode} - {ex.Message}";
                            result.ErrorCount++;
                        }
                        catch (Exception ex)
                        {
                            itemResult.ErrorMessage = ex.Message;
                            result.ErrorCount++;
                        }
                        finally
                        {
                            if (handleCreated)
                            {
                                try { adsClient.DeleteVariableHandle(handle); } catch { /* best effort */ }
                            }
                        }

                        result.Items.Add(itemResult);
                    }
                }

                // The batch ran. Top-level Success reflects that; per-item
                // failures are in Items. Writes are NOT atomic — a partial
                // failure leaves earlier writes applied, so flag it loudly.
                result.Success = true;
                if (result.ErrorCount > 0)
                    result.Warning = $"{result.ErrorCount} of {result.SymbolCount} writes failed; " +
                                     "earlier writes in the batch are NOT rolled back";

                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 0;
            }
            catch (AdsErrorException ex)
            {
                result.ErrorMessage = $"ADS Error: {ex.ErrorCode} - {ex.Message}";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 1;
            }
            catch (Exception ex)
            {
                result.ErrorMessage = ex.Message;
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true }));
                return 1;
            }
        }

        private static object ReadTypedValue(AdsClient client, uint handle, string typeName, int size)
        {
            string upperType = typeName.ToUpperInvariant();

            if (upperType == "BOOL")
                return client.ReadAny(handle, typeof(bool));
            if (upperType == "BYTE" || upperType == "USINT")
                return client.ReadAny(handle, typeof(byte));
            if (upperType == "SINT")
                return client.ReadAny(handle, typeof(sbyte));
            if (upperType == "WORD" || upperType == "UINT")
                return client.ReadAny(handle, typeof(ushort));
            if (upperType == "INT")
                return client.ReadAny(handle, typeof(short));
            if (upperType == "DWORD" || upperType == "UDINT")
                return client.ReadAny(handle, typeof(uint));
            if (upperType == "DINT")
                return client.ReadAny(handle, typeof(int));
            if (upperType == "LWORD" || upperType == "ULINT")
                return client.ReadAny(handle, typeof(ulong));
            if (upperType == "LINT")
                return client.ReadAny(handle, typeof(long));
            if (upperType == "REAL")
                return client.ReadAny(handle, typeof(float));
            if (upperType == "LREAL")
                return client.ReadAny(handle, typeof(double));
            if (upperType.StartsWith("STRING"))
                return client.ReadAny(handle, typeof(string), new int[] { size });

            // For arrays and structs, read as byte array
            byte[] data = new byte[size];
            client.Read(handle, data.AsMemory());
            return BitConverter.ToString(data).Replace("-", " ");
        }

        private static void WriteTypedValue(AdsClient client, uint handle, string typeName, int size, string value)
        {
            string upperType = typeName.ToUpperInvariant();

            if (upperType == "BOOL")
            {
                client.WriteAny(handle, ParseBool(value, typeName));
            }
            else if (upperType == "BYTE" || upperType == "USINT")
            {
                client.WriteAny(handle, ParseInt<byte>(value, typeName, byte.TryParse));
            }
            else if (upperType == "SINT")
            {
                client.WriteAny(handle, ParseInt<sbyte>(value, typeName, sbyte.TryParse));
            }
            else if (upperType == "WORD" || upperType == "UINT")
            {
                client.WriteAny(handle, ParseInt<ushort>(value, typeName, ushort.TryParse));
            }
            else if (upperType == "INT")
            {
                client.WriteAny(handle, ParseInt<short>(value, typeName, short.TryParse));
            }
            else if (upperType == "DWORD" || upperType == "UDINT")
            {
                client.WriteAny(handle, ParseInt<uint>(value, typeName, uint.TryParse));
            }
            else if (upperType == "DINT")
            {
                client.WriteAny(handle, ParseInt<int>(value, typeName, int.TryParse));
            }
            else if (upperType == "LWORD" || upperType == "ULINT")
            {
                client.WriteAny(handle, ParseInt<ulong>(value, typeName, ulong.TryParse));
            }
            else if (upperType == "LINT")
            {
                client.WriteAny(handle, ParseInt<long>(value, typeName, long.TryParse));
            }
            else if (upperType == "REAL")
            {
                if (!float.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var f))
                    throw new ArgumentException($"'{value}' is not a valid {typeName} (REAL)");
                client.WriteAny(handle, f);
            }
            else if (upperType == "LREAL")
            {
                if (!double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var d))
                    throw new ArgumentException($"'{value}' is not a valid {typeName} (LREAL)");
                client.WriteAny(handle, d);
            }
            else if (upperType.StartsWith("STRING"))
            {
                client.WriteAny(handle, value, new int[] { size });
            }
            else
            {
                throw new ArgumentException($"Unsupported type for writing: {typeName}");
            }
        }

        private delegate bool TryParseHandler<T>(string s, NumberStyles styles, IFormatProvider provider, out T result);

        private static T ParseInt<T>(string value, string typeName, TryParseHandler<T> tryParse)
        {
            if (!tryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed))
                throw new ArgumentException($"'{value}' is not a valid {typeName} (out of range or not an integer)");
            return parsed;
        }

        private static bool ParseBool(string value, string typeName)
        {
            if (bool.TryParse(value, out var b)) return b;       // "true"/"false" (any case)
            if (value == "1") return true;
            if (value == "0") return false;
            throw new ArgumentException($"'{value}' is not a valid {typeName} (BOOL). Use true/false or 1/0");
        }
    }

    public class WriteVariableListResult
    {
        public string AmsNetId { get; set; } = "";
        public int Port { get; set; }
        public bool Success { get; set; }
        public int SymbolCount { get; set; }
        public int SuccessCount { get; set; }
        public int ErrorCount { get; set; }
        // Ordered list (not a dictionary) so request order is preserved.
        public List<WriteVariableItemResult> Items { get; set; } = new List<WriteVariableItemResult>();
        public string? Warning { get; set; }
        public string? ErrorMessage { get; set; }
    }

    public class WriteVariableItemResult
    {
        public string Symbol { get; set; } = "";
        public bool Success { get; set; }
        public string PreviousValue { get; set; } = "";
        public string NewValue { get; set; } = "";
        public string DataType { get; set; } = "";
        public string? ErrorMessage { get; set; }
    }
}
