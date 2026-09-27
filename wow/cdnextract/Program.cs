// cdnextract — pull DB2 tables for one build straight from Blizzard's CDN and
// write them as CSVs shaped exactly like wago.tools' /db2/<Table>/csv export.
//
// This is the engine behind `python -m ingest.run --source cdn` (see
// ingest/cdn_client.py). It exists as a PARITY FALLBACK for when wago.tools
// lags behind a new build. It is NOT a way past Blizzard's withheld-key
// encryption: rows in DB2 sections whose TACT key isn't public are skipped,
// exactly as wago.tools skips them.
//
// Engine: TACTSharp (CDN/TACT/BLTE) + DBCD with WoWDBDefs — the same
// libraries wow.tools.local / wago.tools use, for parse parity on new builds.
//
// Usage:
//   cdnextract --product wow_classic_beta --region us
//              --buildconfig <hex> --cdnconfig <hex> [--productconfig <hex>]
//              --version 1.60.1.70009 --defs <WoWDBDefs/definitions>
//              --manifest <WoWDBDefs/manifest.json> --keys <TACTKeys WoW.txt>
//              --cache <dir> --out <dir> Table1 Table2 ...
//
// Writes <out>/<Table>.csv for each table (atomically) and <out>/_cdn_extract.json
// with per-table row counts and skipped encrypted sections. Exit code 1 if any
// requested table fails.

using System.Globalization;
using System.Reflection;
using System.Text;
using System.Text.Json;
using DBCD;
using DBCD.IO.Attributes;
using DBCD.Providers;
using TACTSharp;

static class Program
{
    static int Main(string[] argv)
    {
        var opts = new Dictionary<string, string>();
        var tables = new List<string>();
        for (int i = 0; i < argv.Length; i++)
        {
            if (argv[i].StartsWith("--"))
                opts[argv[i][2..]] = argv[++i];
            else
                tables.Add(argv[i]);
        }

        string Req(string k) => opts.TryGetValue(k, out var v) ? v : throw new ArgumentException($"--{k} is required");

        var version = Req("version");
        var outDir = Req("out");
        Directory.CreateDirectory(outDir);

        // TACT keys (community list, wowdev/TACTKeys). Anything not in here stays
        // encrypted; BLTE leaves those blocks zeroed and DBCD drops the section.
        int keyCount = 0;
        foreach (var line in File.ReadAllLines(Req("keys")))
        {
            var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length < 2 || parts[1].Length != 32) continue;
            KeyService.SetKey(ulong.Parse(parts[0], NumberStyles.HexNumber), Convert.FromHexString(parts[1]));
            keyCount++;
        }
        Log($"loaded {keyCount} TACT keys");

        // Table name -> DB2 FileDataID from WoWDBDefs' manifest (no 100MB listfile needed).
        var fdids = new Dictionary<string, uint>(StringComparer.OrdinalIgnoreCase);
        using (var doc = JsonDocument.Parse(File.ReadAllText(Req("manifest"))))
        {
            foreach (var entry in doc.RootElement.EnumerateArray())
            {
                if (entry.TryGetProperty("db2FileDataID", out var id) && id.ValueKind == JsonValueKind.Number && id.GetUInt32() != 0)
                    fdids[entry.GetProperty("tableName").GetString()!] = id.GetUInt32();
            }
        }

        Settings.LogLevel = TSLogLevel.Warn;
        var build = new BuildInstance();
        build.Settings.Product = Req("product");
        build.Settings.Region = opts.GetValueOrDefault("region", "us");
        build.Settings.CacheDir = Req("cache");
        if (opts.TryGetValue("cdns", out var extraCdns))
            build.Settings.AdditionalCDNs.AddRange(extraCdns.Split(',', StringSplitOptions.RemoveEmptyEntries));
        build.cdn.ProductDirectory = opts.GetValueOrDefault("cdnpath", "tpr/wow");

        if (opts.TryGetValue("productconfig", out var productConfig) && productConfig.Length == 32)
            build.LoadConfigs(Req("buildconfig"), Req("cdnconfig"), productConfig);
        else
            build.LoadConfigs(Req("buildconfig"), Req("cdnconfig"));

        var buildName = build.BuildConfig!.Values.TryGetValue("build-name", out var bn) ? bn[0] : "?";
        Log($"loading build {buildName} ({version})");
        build.Load();

        var dbcProvider = new BytesDBCProvider();
        var dbcd = new DBCD.DBCD(dbcProvider, new FilesystemDBDProvider(Req("defs")));

        var summary = new Dictionary<string, object>
        {
            ["version"] = version,
            ["build_name"] = buildName,
            ["build_config"] = Req("buildconfig"),
            ["cdn_config"] = Req("cdnconfig"),
            ["tables"] = new Dictionary<string, object>(),
        };
        var tableSummary = (Dictionary<string, object>)summary["tables"];
        int failures = 0;

        foreach (var table in tables)
        {
            try
            {
                if (!fdids.TryGetValue(table, out var fdid))
                    throw new Exception($"no FileDataID for {table} in WoWDBDefs manifest");

                dbcProvider.Data = build.OpenFileByFDID(fdid);
                var storage = dbcd.Load(table, version);

                var encrypted = storage.GetEncryptedSections();
                var rows = WriteCsv(storage, Path.Combine(outDir, table + ".csv"));
                tableSummary[table] = new Dictionary<string, object>
                {
                    ["fdid"] = fdid,
                    ["rows"] = rows,
                    ["encrypted_sections"] = encrypted.Count,
                    // key lookup -> records in that section; a section decrypts only if its key is public
                    ["encrypted_keys"] = encrypted.ToDictionary(e => e.Key.ToString("X16"), e => e.Value),
                };
                Log($"{table}: {rows} rows ({encrypted.Count} encrypted sections)");
            }
            catch (Exception e)
            {
                failures++;
                tableSummary[table] = new Dictionary<string, object> { ["error"] = e.Message };
                Log($"{table}: FAILED — {e.Message}");
            }
        }

        File.WriteAllText(Path.Combine(outDir, "_cdn_extract.json"),
            JsonSerializer.Serialize(summary, new JsonSerializerOptions { WriteIndented = true }));
        return failures == 0 ? 0 : 1;
    }

    static void Log(string msg) => Console.Error.WriteLine($"[cdnextract] {msg}");

    // wago.tools CSV shape: DBD column order, arrays expanded to Name_0..Name_N,
    // non-empty strings always quoted, empty strings bare, invariant numbers.
    static int WriteCsv(IDBCDStorage storage, string path)
    {
        var rowType = storage.GetType().GetGenericArguments()[0];
        var cardinality = rowType.GetFields()
            .Where(f => f.FieldType.IsArray)
            .ToDictionary(f => f.Name, f => f.GetCustomAttribute<CardinalityAttribute>()!.Count);

        var columns = storage.AvailableColumns;
        var header = new List<string>();
        foreach (var col in columns)
        {
            var name = WagoColumnNames.GetValueOrDefault(col, col);
            if (cardinality.TryGetValue(col, out var n))
                for (int i = 0; i < n; i++) header.Add($"{name}_{i}");
            else
                header.Add(name);
        }

        var tmp = path + ".tmp";
        int count = 0;
        using (var w = new StreamWriter(tmp, false, new UTF8Encoding(false)))
        {
            w.NewLine = "\n";
            w.WriteLine(string.Join(",", header));
            var sb = new StringBuilder();
            // DB2 record order, like wago.tools (not sorted by ID).
            foreach (var row in ((IDictionary<int, DBCDRow>)storage).Values)
            {
                sb.Clear();
                bool first = true;
                foreach (var col in columns)
                {
                    if (cardinality.ContainsKey(col))
                    {
                        foreach (var v in (Array)row[col])
                        {
                            if (!first) sb.Append(',');
                            sb.Append(Format(v));
                            first = false;
                        }
                    }
                    else
                    {
                        if (!first) sb.Append(',');
                        sb.Append(Format(row[col]));
                        first = false;
                    }
                }
                w.WriteLine(sb.ToString());
                count++;
            }
        }
        File.Move(tmp, path, true);
        return count;
    }

    // DBD column names wago.tools renames in its CSV export (SQL keywords, presumably).
    static readonly Dictionary<string, string> WagoColumnNames = new() { ["Index"] = "_Index" };

    static string Format(object? v) => v switch
    {
        null => "",
        string s when s.Length == 0 => "",
        string s => "\"" + s.Replace("\"", "\"\"") + "\"",
        float f => FormatFloat(f),
        double d => FormatFloat(d),
        Enum e => Convert.ToInt64(e).ToString(CultureInfo.InvariantCulture),
        IFormattable x => x.ToString(null, CultureInfo.InvariantCulture),
        _ => v.ToString() ?? "",
    };

    // wago.tools prints floats PHP-style: widen to double, round(x, 11), then
    // %.14G with a bare exponent and a mantissa that always has a decimal —
    // 0.2f -> 0.20000000298, 1009.58f -> 1009.5800170898, 8.8e-5f -> 8.8E-5,
    // 9e-5f -> 9.0E-5, 1e17f -> 9.9999998430675E+16.
    static string FormatFloat(double d)
    {
        d = Math.Round(d, 11, MidpointRounding.AwayFromZero);
        var s = d.ToString("G14", CultureInfo.InvariantCulture);
        int e = s.IndexOf('E');
        if (e < 0) return s;
        var mantissa = s[..e];
        if (!mantissa.Contains('.')) mantissa += ".0";
        var digits = s[(e + 2)..].TrimStart('0');
        return mantissa + "E" + s[e + 1] + (digits.Length == 0 ? "0" : digits);
    }

    sealed class BytesDBCProvider : IDBCProvider
    {
        public byte[] Data = [];
        public Stream StreamForTableName(string tableName, string build) => new MemoryStream(Data);
    }
}
