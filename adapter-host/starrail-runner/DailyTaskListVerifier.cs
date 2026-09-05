using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;

namespace YeYuGamer.StarRailAdapter
{
    internal sealed class DailyVisualEvidenceFile
    {
        public string Path;
        public string FileName;
        public string Kind;
        public string MimeType;
    }

    internal sealed class DailyTaskListVerification
    {
        public bool Activity500Confirmed;
        public bool AllRewardTiersClaimed;
        public string ReasonCode;
        public string Reason;
        public List<DailyVisualEvidenceFile> EvidenceFiles = new List<DailyVisualEvidenceFile>();

        public bool Complete
        {
            get { return Activity500Confirmed && AllRewardTiersClaimed; }
        }
    }

    internal sealed class DailyEvidenceException : Exception
    {
        public string Code { get; private set; }

        public DailyEvidenceException(string code, string message) : base(message)
        {
            Code = code;
        }
    }

    // This verifier deliberately consumes only Manager-owned files for the current
    // run attempt. March7th logs remain diagnostics and cannot satisfy this contract.
    internal static class DailyTaskListVerifier
    {
        private const string EvidencePrefix = "starrail-daily-task-list-";
        private const string EvidenceType = "starrail.daily-task-list.visual@1.0";
        private const string Producer = "yeyu-gamer-starrail-observer";
        private const string DetectorVersion = "starrail-daily-task-list-v1";
        private const string Scene = "daily-training-task-list";
        private const long MaximumSidecarBytes = 64 * 1024;
        private const long MaximumImageBytes = 20L * 1024 * 1024;
        private static readonly int[] RewardThresholds = { 100, 200, 300, 400, 500 };

        public static DailyTaskListVerification Inspect(string stagingRoot, ExecuteRequest request)
        {
            string[] candidates;
            try
            {
                candidates = Directory.GetFiles(stagingRoot, EvidencePrefix + "*.ocr.json", SearchOption.TopDirectoryOnly);
            }
            catch
            {
                return Result("daily_visual_evidence_unreadable", "The current-attempt evidence directory could not be read.");
            }
            Array.Sort(candidates, StringComparer.Ordinal);
            if (candidates.Length == 0)
                return Result("daily_visual_evidence_missing", "No current-attempt StarRail daily-task PNG/OCR sidecar pair was available.");
            if (candidates.Length != 1)
                return Result("daily_visual_evidence_ambiguous", "Exactly one current-attempt StarRail daily-task OCR sidecar is required.");

            List<DailyVisualEvidenceFile> files = new List<DailyVisualEvidenceFile>();
            string sidecarPath = candidates[0];
            try
            {
                FileInfo sidecarInfo = new FileInfo(sidecarPath);
                if (!sidecarInfo.Exists || sidecarInfo.Length <= 0 || sidecarInfo.Length > MaximumSidecarBytes || StrictJson.IsReparse(sidecarPath))
                    throw new DailyEvidenceException("daily_visual_sidecar_invalid", "The OCR sidecar is missing, oversized, or unsafe.");
                files.Add(new DailyVisualEvidenceFile
                {
                    Path = sidecarPath,
                    FileName = sidecarInfo.Name,
                    Kind = "starrail-daily-task-list-ocr",
                    MimeType = "text/plain"
                });

                byte[] sidecarBytes = File.ReadAllBytes(sidecarPath);
                IDictionary<string, object> value = StrictJson.Object(StrictJson.Utf8(sidecarBytes, "daily-task OCR sidecar"),
                    "daily-task OCR sidecar", (int)MaximumSidecarBytes);
                StrictJson.ExactKeys(value, new string[]
                {
                    "schemaVersion", "evidenceType", "producer", "detectorVersion", "captureId",
                    "runId", "runAttemptId", "capturedAt", "scene", "imageFileName",
                    "imageSha256", "activity", "rewardTiers"
                }, "daily-task OCR sidecar");
                if (StrictJson.Integer(value["schemaVersion"], "schemaVersion", 1, 1) != 1 ||
                    StrictJson.Text(value["evidenceType"], "evidenceType", 128) != EvidenceType ||
                    StrictJson.Text(value["producer"], "producer", 128) != Producer ||
                    StrictJson.Text(value["detectorVersion"], "detectorVersion", 128) != DetectorVersion ||
                    StrictJson.Text(value["scene"], "scene", 64) != Scene)
                    throw new DailyEvidenceException("daily_visual_sidecar_invalid", "The OCR sidecar identity differs from the product-owned evidence contract.");

                string captureId = StrictJson.CanonicalUuid(value["captureId"], "captureId");
                string expectedSidecar = EvidencePrefix + captureId + ".ocr.json";
                string expectedImage = EvidencePrefix + captureId + ".png";
                if (!String.Equals(sidecarInfo.Name, expectedSidecar, StringComparison.Ordinal))
                    throw new DailyEvidenceException("daily_visual_sidecar_invalid", "The OCR sidecar name does not match its capture ID.");
                if (StrictJson.CanonicalUuid(value["runId"], "runId") != request.RunId ||
                    StrictJson.CanonicalUuid(value["runAttemptId"], "runAttemptId") != request.RunAttemptId)
                    throw new DailyEvidenceException("daily_visual_evidence_scope_mismatch", "The visual evidence belongs to another run attempt.");

                string imageFileName = StrictJson.Leaf(value["imageFileName"], "imageFileName");
                if (!String.Equals(imageFileName, expectedImage, StringComparison.Ordinal))
                    throw new DailyEvidenceException("daily_visual_sidecar_invalid", "The OCR sidecar image name does not match its capture ID.");
                string imagePath;
                try { imagePath = StrictJson.ContainedFile(stagingRoot, imageFileName); }
                catch (RunnerValidationException)
                {
                    throw new DailyEvidenceException("daily_visual_image_missing", "The OCR sidecar's PNG is missing or unsafe.");
                }
                FileInfo imageInfo = new FileInfo(imagePath);
                if (imageInfo.Length <= 0 || imageInfo.Length > MaximumImageBytes)
                    throw new DailyEvidenceException("daily_visual_image_invalid", "The daily-task PNG size is invalid.");
                files.Add(new DailyVisualEvidenceFile
                {
                    Path = imagePath,
                    FileName = imageFileName,
                    Kind = "game-ui-daily-task-list",
                    MimeType = "image/png"
                });

                string expectedHash = StrictJson.Text(value["imageSha256"], "imageSha256", 64);
                if (!Regex.IsMatch(expectedHash, "^[0-9a-f]{64}$", RegexOptions.CultureInvariant) ||
                    StrictJson.Sha256File(imagePath) != expectedHash)
                    throw new DailyEvidenceException("daily_visual_image_mismatch", "The daily-task PNG differs from the OCR sidecar digest.");
                ValidatePng(imagePath);

                DateTimeOffset capturedAt = StrictJson.Time(value["capturedAt"], "capturedAt");
                DateTimeOffset now = DateTimeOffset.UtcNow;
                if (capturedAt < request.IssuedAt || capturedAt > request.ExpiresAt || capturedAt > now.AddSeconds(5))
                    throw new DailyEvidenceException("daily_visual_evidence_stale", "The PNG/OCR pair was not captured during the current execution lease.");

                IDictionary<string, object> activity = StrictJson.AsObject(value["activity"], "activity");
                StrictJson.ExactKeys(activity, new string[] { "current", "target", "ocrText" }, "activity");
                int current = checked((int)StrictJson.Integer(activity["current"], "activity.current", 0, 500));
                int target = checked((int)StrictJson.Integer(activity["target"], "activity.target", 1, 500));
                string activityText = StrictJson.Text(activity["ocrText"], "activity.ocrText", 512);
                bool activityConfirmed = current == 500 && target == 500 &&
                    Regex.IsMatch(activityText, @"(^|[^0-9])500\s*/\s*500([^0-9]|$)", RegexOptions.CultureInvariant);
                if (!activityConfirmed)
                    return Result("daily_training_visual_unconfirmed", "The current-attempt visual evidence does not strictly confirm 500/500 activity.", files, false, false);

                List<object> rewards = StrictJson.Array(value["rewardTiers"], "rewardTiers", 0, 8);
                if (rewards.Count != RewardThresholds.Length)
                    return Result("daily_reward_tiers_incomplete", "The current-attempt visual evidence does not contain exactly five reward tiers.", files, true, false);
                for (int index = 0; index < RewardThresholds.Length; index++)
                {
                    IDictionary<string, object> reward = StrictJson.AsObject(rewards[index], "reward tier");
                    StrictJson.ExactKeys(reward, new string[] { "threshold", "claimed", "visualState", "ocrText" }, "reward tier");
                    int threshold = checked((int)StrictJson.Integer(reward["threshold"], "reward.threshold", 0, 500));
                    bool claimed = reward["claimed"] is bool && (bool)reward["claimed"];
                    string visualState = StrictJson.Text(reward["visualState"], "reward.visualState", 32);
                    string rewardText = StrictJson.Text(reward["ocrText"], "reward.ocrText", 256);
                    bool thresholdSeen = Regex.IsMatch(rewardText,
                        @"(^|[^0-9])" + RewardThresholds[index].ToString() + @"([^0-9]|$)", RegexOptions.CultureInvariant);
                    if (threshold != RewardThresholds[index] || !claimed || visualState != "claimed" || !thresholdSeen)
                        return Result("daily_reward_tiers_unclaimed", "The current-attempt visual evidence does not confirm all five reward tiers as claimed.", files, true, false);
                }
                return Result("daily_task_list_visual_confirmed", "Current-attempt PNG/OCR evidence confirms 500/500 activity and all five claimed reward tiers.", files, true, true);
            }
            catch (DailyEvidenceException error)
            {
                return Result(error.Code, error.Message, files, false, false);
            }
            catch (RunnerValidationException error)
            {
                return Result("daily_visual_sidecar_invalid", "The OCR sidecar failed strict schema validation: " + error.Code + ".", files, false, false);
            }
            catch
            {
                return Result("daily_visual_evidence_unreadable", "The current-attempt PNG/OCR evidence could not be read safely.", files, false, false);
            }
        }

        private static DailyTaskListVerification Result(string code, string reason)
        {
            return Result(code, reason, new List<DailyVisualEvidenceFile>(), false, false);
        }

        private static DailyTaskListVerification Result(string code, string reason, List<DailyVisualEvidenceFile> files,
            bool activityConfirmed, bool rewardsConfirmed)
        {
            return new DailyTaskListVerification
            {
                ReasonCode = code,
                Reason = reason,
                Activity500Confirmed = activityConfirmed,
                AllRewardTiersClaimed = rewardsConfirmed,
                EvidenceFiles = files
            };
        }

        private static void ValidatePng(string path)
        {
            byte[] signature = new byte[8];
            using (FileStream stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                if (stream.Read(signature, 0, signature.Length) != signature.Length ||
                    !signature.SequenceEqual(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 }))
                    throw new DailyEvidenceException("daily_visual_image_invalid", "The evidence image is not a PNG.");
            }
            try
            {
                using (Bitmap bitmap = new Bitmap(path))
                {
                    if (bitmap.RawFormat.Guid != ImageFormat.Png.Guid || bitmap.Width < 640 || bitmap.Height < 360)
                        throw new DailyEvidenceException("daily_visual_image_invalid", "The daily-task PNG has an invalid format or dimensions.");
                    int minimum = 255;
                    int maximum = 0;
                    for (int y = 1; y <= 10; y++)
                    {
                        for (int x = 1; x <= 10; x++)
                        {
                            Color color = bitmap.GetPixel(x * (bitmap.Width - 1) / 11, y * (bitmap.Height - 1) / 11);
                            int brightness = (color.R + color.G + color.B) / 3;
                            minimum = Math.Min(minimum, brightness);
                            maximum = Math.Max(maximum, brightness);
                        }
                    }
                    if (maximum - minimum < 12)
                        throw new DailyEvidenceException("daily_visual_image_uniform", "The daily-task PNG is visually uniform and cannot prove a UI state.");
                }
            }
            catch (DailyEvidenceException) { throw; }
            catch
            {
                throw new DailyEvidenceException("daily_visual_image_invalid", "The daily-task PNG could not be decoded.");
            }
        }
    }
}
