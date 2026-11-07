using System;
using System.IO;
using System.Text;

namespace TcAutomation.Core
{
    /// <summary>
    /// The MCP server decodes the worker's pipes as strict UTF-8. The console code page is not UTF-8,
    /// and Console.OutputEncoding needs a console, so redirected streams are replaced instead.
    /// </summary>
    public static class ConsoleUtf8
    {
        public static readonly Encoding Encoding = new UTF8Encoding(false);

        public static TextWriter Writer(Stream stream) => new StreamWriter(stream, Encoding) { AutoFlush = true };

        public static void Apply()
        {
            if (Console.IsOutputRedirected) Console.SetOut(Writer(Console.OpenStandardOutput()));
            if (Console.IsErrorRedirected) Console.SetError(Writer(Console.OpenStandardError()));
            if (Console.IsInputRedirected) Console.SetIn(new StreamReader(Console.OpenStandardInput(), Encoding));
        }
    }
}
