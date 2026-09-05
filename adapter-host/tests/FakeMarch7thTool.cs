using System;
using System.IO;
using System.Text;
using System.Threading;

internal static class FakeMarch7thTool
{
    public static int Main(string[] args)
    {
        // The zero-argument form represents the visible GUI/update pass. The
        // documented task form is `March7th Launcher.exe <task> -e`.
        if (args.Length == 0) return 0;
        if (args.Length != 2 || args[1] != "-e") return 64;
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string modePath = Path.Combine(root, "mode.txt");
        string mode = File.Exists(modePath) ? File.ReadAllText(modePath).Trim() : "success";
        string logs = Path.Combine(root, "logs");
        Directory.CreateDirectory(logs);
        File.AppendAllText(Path.Combine(root, "invocations.log"), args[0] + " -e" + Environment.NewLine, new UTF8Encoding(false));
        string logPath = Path.Combine(logs, DateTime.UtcNow.ToString("yyyy-MM-dd") + ".log");
        if (args[0] == "power")
            Append(logPath, "开始清体力\n开始刷拟造花萼（金）\n");
        else if (args[0] == "daily")
            Append(logPath, "开始日常任务\n");
        else if (args[0] == "game")
        {
            Append(logPath, "窗口已切换到前台\n");
            if (mode == "home-lookalike") Append(logPath, "当前界面：主界面设置\n");
            else if (mode != "missing-home") Append(logPath, "当前界面：主界面\n");
        }
        else return 64;
        if (mode == "home-failure" || mode == "home-failure-clean")
        {
            Append(logPath, "当前界面：手机菜单\n切换到 主界面 超时，准备重试\n当前界面：手机菜单\n切换到 主界面 超时，准备重试\n当前界面：手机菜单\n无法切换到 主界面\n发生错误 无法切换到指定游戏界面\n");
            return mode == "home-failure" ? 19 : 0;
        }
        if (mode == "hang")
        {
            Thread.Sleep(TimeSpan.FromMinutes(5));
            return 0;
        }
        if (mode == "failure")
        {
            Append(logPath, args[0] == "power" ? "清体力未完成 fake\n" : "每日实训未完成\n");
            return 0;
        }
        if (mode == "contradictory" && args[0] == "daily")
        {
            Append(logPath, "当前累计分数：500/500\n检测到本日活跃度已满提示\n每日实训奖励完成\n每日实训未完成\n");
            return 0;
        }
        if (mode == "human")
        {
            Append(logPath, "登录状态失效，请重新登录\n");
            return 0;
        }
        if (args[0] == "power")
            Append(logPath, "副本任务完成\n");
        else if (args[0] == "daily")
            Append(logPath, "当前累计分数：500/500\n检测到本日活跃度已满提示\n每日实训奖励完成\n");
        return 0;
    }

    private static void Append(string path, string text)
    {
        File.AppendAllText(path, text, new UTF8Encoding(false));
    }
}
