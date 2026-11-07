using System;
using System.IO;
using System.Linq;
using TcAutomation.Core;

static class ConsoleTests
{
    static int cases;
    static void Check(bool value, string message) { cases++; if (!value) throw new Exception(message); }

    public static void Run()
    {
        using (var stream = new MemoryStream())
        {
            var writer = ConsoleUtf8.Writer(stream);
            writer.Write("Üא");
            Check(stream.ToArray().SequenceEqual(new byte[] { 0xC3, 0x9C, 0xD7, 0x90 }), "Console writer: UTF-8 without a BOM");
        }
        Console.WriteLine("PASS: " + cases + " console encoding cases; no XAE or PLC used.");
    }
}
