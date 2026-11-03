using System;
using System.Text.Json;
using System.Text.Json.Serialization;
using TcAutomation.Commands;
using TcAutomation.Core;

static class PortTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    public static void Run()
    {
        int? Port(string xml) => AutomationInterface.ConfiguredAdsPort(xml);
        Check(Port("<TreeItem><ItemName>PLC1</ItemName><PlcProjectDef><AdsPort>852</AdsPort></PlcProjectDef></TreeItem>") == 852, "Configured port read");
        Check(Port("<TreeItem><PlcProjectDef><AdsPort> 853 </AdsPort></PlcProjectDef></TreeItem>") == 853, "Whitespace around the port");
        Check(Port("<TreeItem><PlcProjectDef /></TreeItem>") == null, "No port: null, not a default");
        Check(Port("<TreeItem><PlcProjectDef><AdsPort>x</AdsPort></PlcProjectDef></TreeItem>") == null, "Unparseable port: null");
        Check(Port("<TreeItem><AmsPort>851</AmsPort></TreeItem>") == null, "An AmsPort element is not the PLC port");
        Check(Port("<TreeItem><TreeItem><PlcProjectDef><AdsPort>854</AdsPort></PlcProjectDef></TreeItem></TreeItem>") == null, "A child's port is not this item's");

        // The host and batch writers use these options.
        var host = new JsonSerializerOptions { PropertyNamingPolicy = JsonNamingPolicy.CamelCase, DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull };
        Check(JsonSerializer.Serialize(new ListPlcInfo { Name = "PLC1" }, host).Contains("\"amsPort\":null"), "list_plcs keeps an unknown port as null");
        Check(JsonSerializer.Serialize(new TcAutomation.Models.PlcInfo { Name = "PLC1" }, host).Contains("\"amsPort\":null"), "info keeps an unknown port as null");
        Console.WriteLine("PASS: " + cases + " ADS port cases; no XAE or PLC used.");
    }
}
