using System;
using System.Collections.Generic;
using System.Reflection;
using System.Web.Script.Serialization;

internal static class ReplayWwAccountBridge {
    public static int Main(string[] args) {
        Type runner = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
        MethodInfo valid = runner.GetMethod("ValidAccountRequest", BindingFlags.NonPublic | BindingFlags.Static);
        MethodInfo patch = runner.GetMethod("PatchWwAccountEntry", BindingFlags.NonPublic | BindingFlags.Static);
        var json = new JavaScriptSerializer();
        string[] documents = {
            "{\"gameId\":\"WW\"}",
            "{\"gameId\":\"WW\",\"accountId\":\"default\",\"accountSnapshot\":{}}",
            "{\"gameId\":\"WW\",\"accountId\":\"11111111-1111-4111-8111-111111111111\",\"accountSnapshot\":{\"label\":\"A\",\"saved_account_label\":\"alpha****example.com\"}}",
            "{\"gameId\":\"GF2\",\"accountId\":\"default\"}",
            "{\"gameId\":\"WW\",\"accountId\":\"../other\"}",
            "{\"gameId\":\"WW\",\"accountId\":\"11111111-1111-4111-8111-111111111111\",\"accountSnapshot\":{}}",
            "{\"gameId\":\"WW\",\"accountSnapshot\":{\"label\":\"A\",\"saved_account_label\":\"alpha****\",\"password\":\"placeholder\"}}"
        };
        for (int i = 0; i < documents.Length; i++) {
            bool result = (bool)valid.Invoke(null, new object[] { json.DeserializeObject(documents[i]) });
            if (result != (i < 3)) throw new Exception("account request replay " + i);
        }
        string source = "if __name__ == '__main__':\n    from config import config\n    from ok import OK\n    ok = OK(config)\n    ok.start()\n";
        string once = (string)patch.Invoke(null, new object[] { source });
        string twice = (string)patch.Invoke(null, new object[] { once });
        if (once != twice || once.IndexOf("_yeyu_install_account_bridge(config, __file__)") > once.IndexOf("ok = OK(config)")) throw new Exception("account entry order/idempotency");
        try {
            patch.Invoke(null, new object[] { source.Replace("ok = OK(config)", "ok = Unknown(config)") });
            throw new Exception("unknown entry accepted");
        } catch (TargetInvocationException error) {
            if (error.InnerException.GetType().Name != "AccountBridgeGate") throw;
        }
        Console.WriteLine("WW account runner replay passed: 9 cases; no tool/game started");
        return 0;
    }
}
