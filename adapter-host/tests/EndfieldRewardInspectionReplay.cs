using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Web.Script.Serialization;

internal static class EndfieldRewardInspectionReplay {
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    public static int Main(string[] args) {
        if (args.Length != 2) return 64;
        var type = Assembly.LoadFrom(Path.GetFullPath(args[0])).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        var flags = BindingFlags.Static | BindingFlags.NonPublic;
        var method = type.GetMethod("EndfieldShouldUseRewardInspection", flags);
        var request = new Dictionary<string, object>();
        Func<string, string, string[], bool, bool> allowed = (game, account, operations, supported) =>
            (bool)method.Invoke(null, new object[] {game, account, operations, request, supported});
        if (allowed("Endfield", "default", new [] {"claim-daily-reward"}, true)) return 65;
        request["observationOnlyOperations"] = new object[0];
        if (allowed("Endfield", "default", new [] {"claim-daily-reward"}, true)) return 66;
        request["observationOnlyOperations"] = new object[] {"claim-daily-reward"};
        if (!allowed("Endfield", "default", new [] {"claim-daily-reward"}, true) ||
            !allowed("Endfield", "default", new [] {"attach-world", "claim-daily-reward"}, true)) return 67;
        var cases = new object[][] {
            new object[] {"Endfield", "default", new [] {"spend-sanity", "claim-daily-reward"}, true},
            new object[] {"Endfield", "default", new [] {"attach-world"}, true},
            new object[] {"GF2", "default", new [] {"claim-daily-reward"}, true},
            new object[] {"Endfield", "another-account", new [] {"claim-daily-reward"}, true},
            new object[] {"Endfield", "default", new [] {"claim-daily-reward"}, false}
        };
        foreach (var test in cases) {
            try { allowed((string)test[0], (string)test[1], (string[])test[2], (bool)test[3]); return 68; }
            catch (TargetInvocationException error) { if (!(error.InnerException is InvalidDataException)) throw; }
        }
        foreach (var bad in new object[] {null, "claim-daily-reward", new object[] {"claim-daily-reward", "claim-daily-reward"}}) {
            request["observationOnlyOperations"] = bad;
            try { allowed("Endfield", "default", new [] {"claim-daily-reward"}, true); return 69; }
            catch (TargetInvocationException error) { if (!(error.InnerException is InvalidDataException)) throw; }
        }
        var record = Json.DeserializeObject(File.ReadAllText(Path.Combine(args[1], "Endfield", "runtime-preparation.json"))) as IDictionary<string, object>;
        var source = record["source"] as IDictionary<string, object>;
        var audit = type.GetMethod("EndfieldInspectionSourceMatches", flags);
        if (!(bool)audit.Invoke(null, new object[] {args[1], source})) return 70;
        source["commit"] = "unreviewed-source";
        if ((bool)audit.Invoke(null, new object[] {args[1], source})) return 71;
        Console.WriteLine("Endfield inspection scope and real candidate integrity passed; no process started.");
        return 0;
    }
}
