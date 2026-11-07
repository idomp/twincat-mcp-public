using System;
using System.Collections.Generic;
using System.Text.Json;
using TwinCAT.Ads;
using TcAutomation.Core;

namespace TcAutomation.Commands
{
    public static class ReadVariableListCommand
    {
        public static int Execute(string amsNetId, int port, string[] symbols)
        {
            var result = new ReadVariableListResult
            {
                AmsNetId = amsNetId,
                Port = port
            };

            if (symbols == null || symbols.Length == 0)
            {
                result.ErrorMessage = "No symbols specified";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                return 1;
            }

            if (symbols.Length > 500)
            {
                result.ErrorMessage = "Maximum 500 symbols per batch";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                return 1;
            }

            result.SymbolCount = symbols.Length;

            try
            {
                using (var adsClient = new AdsClient())
                {
                    adsClient.Connect(amsNetId, port);

                    if (!adsClient.IsConnected)
                    {
                        result.ErrorMessage = $"Failed to connect to {amsNetId}:{port}";
                        Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                        return 1;
                    }

                    int remainingBytes = 8 * 1024 * 1024;
                    var stateInfo = adsClient.ReadState();
                    if (!AdsValues.Readable(stateInfo.AdsState))
                    {
                        result.ErrorMessage = $"PLC is not in Run or Stop (state: {stateInfo.AdsState}). Cannot read variables.";
                        Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                        return 1;
                    }

                    foreach (var symbolName in symbols)
                    {
                        var itemResult = new ReadVariableItemResult();
                        uint handle = 0;
                        bool handleCreated = false;

                        try
                        {
                            var symbolInfo = adsClient.ReadSymbol(symbolName);
                            AdsValues.ValidateSize(symbolInfo.Size);
                            if (symbolInfo.Size > remainingBytes) throw new ArgumentException("Batch symbol data exceeds 8 MiB.");
                            remainingBytes -= symbolInfo.Size;
                            itemResult.DataType = symbolInfo.TypeName;
                            itemResult.Size = symbolInfo.Size;

                            handle = adsClient.CreateVariableHandle(symbolName);
                            handleCreated = true;

                            object value = ReadTypedValue(adsClient, handle, symbolInfo.TypeName, symbolInfo.Size, out var bytes);
                            itemResult.Value = AdsValues.Text(value);
                            itemResult.RawValue = value;
                            if (bytes != null)
                            {
                                var node = AdsTypedRead.Describe(symbolInfo);
                                itemResult.TypedValue = AdsTypedRead.Text(node, bytes, out var unit);
                                itemResult.TypedUnit = unit;
                            }
                            itemResult.Success = true;
                        }
                        catch (AdsErrorException ex)
                        {
                            itemResult.Success = false;
                            itemResult.ErrorMessage = $"ADS Error: {ex.ErrorCode} - {ex.Message}";
                        }
                        catch (Exception ex)
                        {
                            itemResult.Success = false;
                            itemResult.ErrorMessage = ex.Message;
                        }
                        finally
                        {
                            if (handleCreated)
                                try { adsClient.DeleteVariableHandle(handle); }
                                catch (Exception cleanup) { itemResult.CleanupError = cleanup.Message; }
                        }

                        result.Add(symbolName, itemResult);
                    }
                }

                foreach (var item in System.Linq.Enumerable.Select(result.Items, i => i.Result))
                {
                    if (item.Success) result.SuccessCount++;
                    else result.ErrorCount++;
                }

                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                return result.Success ? 0 : 1;
            }
            catch (AdsErrorException ex)
            {
                result.ErrorMessage = $"ADS Error: {ex.ErrorCode} - {ex.Message}";
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                return 1;
            }
            catch (Exception ex)
            {
                result.ErrorMessage = ex.Message;
                Console.WriteLine(JsonSerializer.Serialize(result, new JsonSerializerOptions { WriteIndented = true, NumberHandling = System.Text.Json.Serialization.JsonNumberHandling.AllowNamedFloatingPointLiterals }));
                return 1;
            }
        }

        private static object ReadTypedValue(AdsClient client, uint handle, string typeName, int size, out byte[] bytes)
        {
            bytes = null;
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

            byte[] data = new byte[size];
            client.Read(handle, data.AsMemory());
            bytes = data;
            return BitConverter.ToString(data).Replace("-", " ");
        }
    }

    public class ReadVariableListResult
    {
        // Items keeps every requested read in order, duplicates included. Results keeps the last read of each symbol.
        public bool Success => ErrorMessage == null && SymbolCount > 0 && Items.Count == SymbolCount && ErrorCount == 0;
        public List<ReadVariableListItem> Items { get; set; } = new List<ReadVariableListItem>();
        public void Add(string symbol, ReadVariableItemResult item)
        {
            Items.Add(new ReadVariableListItem { Index = Items.Count, Symbol = symbol, Result = item });
            Results[symbol] = item;
        }
        public string AmsNetId { get; set; } = "";
        public int Port { get; set; }
        public int SymbolCount { get; set; }
        public int SuccessCount { get; set; }
        public int ErrorCount { get; set; }
        public Dictionary<string, ReadVariableItemResult> Results { get; set; } = new Dictionary<string, ReadVariableItemResult>();
        public string? ErrorMessage { get; set; }
    }

    public class ReadVariableListItem
    {
        public int Index { get; set; }
        public string Symbol { get; set; } = "";
        public ReadVariableItemResult Result { get; set; }
    }

    public class ReadVariableItemResult
    {
        public string CleanupError { get; set; }
        public bool Success { get; set; }
        public string Value { get; set; } = "";
        public object? RawValue { get; set; }
        public string DataType { get; set; } = "";
        public int Size { get; set; }
        public string? TypedValue { get; set; }
        public string? TypedUnit { get; set; }
        public string? ErrorMessage { get; set; }
    }
}
