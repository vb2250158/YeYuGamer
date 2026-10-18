// Validate the actual runner's frozen inspection selection without game input.
using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;

internal static class Gf2RewardInspectionReplay
{
    static MethodInfo Select;
    static int Count;
    static void Expect(string account, string[] selected, object observations, bool supported, bool accepted)
    {
        var request = new Dictionary<string, object>();
        if (observations != null) request["observationOnlyOperations"] = observations;
        try {
            bool result = (bool)Select.Invoke(null, new object[] { account, selected, request, supported });
            if (result != accepted) throw new Exception("GF2 readonly scope returned an unexpected decision");
        } catch (TargetInvocationException error) {
            if (accepted || !(error.InnerException is InvalidDataException)) throw;
        }
        Count++;
    }
    static int Main(string[] args)
    {
        try {
            if (args.Length != 1 && args.Length != 2) return 64;
            var program = Assembly.LoadFrom(args[0]).GetType("YeYuGamer.LocalDailyAdapter.Program", true);
            Select = program.GetMethod("Gf2ShouldUseRewardInspection", BindingFlags.NonPublic | BindingFlags.Static);
            if (Select == null) return 65;
            string daily = "claim-daily-missions", pass = "battle-pass-free-track";
            Expect("default", new[] { daily }, new object[] { daily }, true, true);
            Expect("default", new[] { "attach-home", daily, pass }, new object[] { daily, pass }, true, true);
            Expect("default", new[] { pass }, new object[] { pass }, true, true);
            Expect("default", new[] { daily }, null, true, false);
            Expect("named-account", new[] { daily }, new object[] { daily }, true, false);
            Expect("default", new[] { daily }, new object[] { daily }, false, false);
            Expect("default", new[] { daily }, new object[] { daily, daily }, true, false);
            Expect("default", new[] { daily }, new object[] { pass }, true, false);
            Expect("default", new[] { daily, "spend-stamina" }, new object[] { daily }, true, false);
            Expect("default", new[] { daily }, new object[] { "spend-stamina" }, true, false);
            Expect("default", new[] { daily }, new object[] { 1 }, true, false);
            Expect("default", new[] { daily }, new object[0], true, false);
            Expect("default", new[] { daily }, daily, true, false);
            var parse = program.GetMethod("ParseStepArtifactDescriptor", BindingFlags.NonPublic | BindingFlags.Static);
            string png = "file=gf2-reward-status-a.png; kind=game-ui-recognition-raw; capturedAt=2026-10-15T02:16:32+08:00";
            string meta = "file=gf2-reward-status-a.json; kind=game-ui-recognition-meta; capturedAt=2026-10-15T02:16:32+08:00";
            var imageDescriptor = (Tuple<string, string, string>)parse.Invoke(null, new object[] { "GF2", true, png });
            var metaDescriptor = (Tuple<string, string, string>)parse.Invoke(null, new object[] { "GF2", true, meta });
            if (imageDescriptor.Item3 != "image/png" || metaDescriptor.Item3 != "text/plain")
                throw new Exception("GF2 raw and metadata must reach artifact_staged with their correct MIME types");
            foreach (object[] rejected in new[] {
                new object[] { "GF2", false, meta }, new object[] { "Endfield", true, meta },
                new object[] { "GF2", true, meta.Replace("file=gf2", "file=../gf2") },
                new object[] { "GF2", true, "{\"file\":\"gf2-reward-status-a.png\",\"kind\":\"game-ui-recognition-raw\"}" },
                new object[] { "GF2", true, png.Replace("game-ui-recognition-raw", "unknown-kind") }
            }) {
                if (parse.Invoke(null, rejected) != null) throw new Exception("Invalid GF2 evidence descriptor accepted");
            }
            Console.WriteLine("GF2 real runner artifact descriptor regression checks: 7");
            if (args.Length == 2) {
                string fixture = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[0])), "gf2-binding-only-fixture.exe");
                File.WriteAllText(fixture, "not executable; binding validation only");
                var binding = program.GetMethod("MakeFormalGuiBinding", BindingFlags.NonPublic | BindingFlags.Static).Invoke(
                    null, new object[] { "GF2", fixture, Path.GetFullPath(args[1]), null });
                if (!(bool)binding.GetType().GetField("Gf2RewardInspectionSupported").GetValue(binding))
                    throw new Exception("Prepared GF2 runtime did not expose the audited read-only capability");
                Console.WriteLine("GF2 prepared runtime exact-source capability binding passed; gameStarted=false");
            }
            Console.WriteLine("GF2 readonly runner scope checks: " + Count);
            return 0;
        } catch (Exception error) {
            Console.Error.WriteLine(error.Message);
            return 1;
        }
    }
}
