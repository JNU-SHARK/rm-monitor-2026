package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/pkg/errors"
	"scutbot.cn/web/rm-monitor/ent"
	"scutbot.cn/web/rm-monitor/ent/matchround"
	"scutbot.cn/web/rm-monitor/ent/mediaartifact"
	"scutbot.cn/web/rm-monitor/ent/recordtask"
	"scutbot.cn/web/rm-monitor/pkg/app"
	"scutbot.cn/web/rm-monitor/pkg/db"
	"scutbot.cn/web/rm-monitor/pkg/logx"
	"scutbot.cn/web/rm-monitor/pkg/storagepath"
	"scutbot.cn/web/rm-monitor/record-job/internal/config"
)

var (
	configFile      = flag.String("f", "etc/config.yml", "the config file")
	taskIDFlag      = flag.Int("task", 0, "record task id")
	mergeTaskIDFlag = flag.Int("merge-task", 0, "merge record part task id")
)

const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
const networkReadTimeoutMicros = "15000000"
const partRoleMarker = "__part"

func init() {
	logx.MustSetup(logx.LogConf{ServiceName: "record-job", Mode: "console", Encoding: "plain"})
}

func main() {
	flag.Parse()
	if *taskIDFlag == 0 && *mergeTaskIDFlag == 0 {
		logx.Error("task id is required")
		os.Exit(1)
	}
	var c config.Config
	app.MustLoadConfig(*configFile, &c)
	client, err := db.Open(context.Background(), c.PostgresConf)
	if err != nil {
		logx.Error(err)
		os.Exit(1)
	}
	defer client.Close()
	var runErr error
	if *mergeTaskIDFlag != 0 {
		runErr = runMerge(context.Background(), client, c, *mergeTaskIDFlag)
	} else {
		runErr = run(context.Background(), client, c, *taskIDFlag)
	}
	if runErr != nil {
		logx.Error(runErr)
		os.Exit(1)
	}
}

func run(ctx context.Context, client *ent.Client, c config.Config, taskID int) error {
	task, err := loadTask(ctx, client, taskID)
	if err != nil {
		return errors.Wrap(err, "get record task")
	}
	conf := c.RecordConf.WithDefaults()
	fullPath := storagepath.Resolve(conf.BaseDir, task.OutputPath)
	if err := os.MkdirAll(filepath.Dir(fullPath), 0o755); err != nil {
		return errors.Wrap(err, "create output dir")
	}

	jobCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	var stopRequested atomic.Bool
	go watchCancel(jobCtx, client, taskID, &stopRequested, cancel)

	if err := client.RecordTask.UpdateOneID(taskID).SetStatus(recordtask.StatusRUNNING).SetStartedAt(time.Now()).Exec(ctx); err != nil {
		return errors.Wrap(err, "mark running")
	}
	logx.Infof("record task started task_id=%d role=%s output=%s", taskID, task.Role, path.Clean(task.OutputPath))

	args := []string{
		"-hide_banner",
		"-loglevel", "info",
	}
	if isNetworkSource(task.SourceURL) {
		args = append(args,
			"-user_agent", UA,
			"-rw_timeout", networkReadTimeoutMicros,
			"-reconnect", "1",
			"-reconnect_streamed", "1",
			"-reconnect_delay_max", "5",
		)
	}
	args = append(args,
		"-i", task.SourceURL,
		"-map", "0:v:0",
		"-map", "0:a:0?",
		"-sn",
		"-dn",
		"-c:v", "copy",
		"-c:a", "copy",
		"-f", "flv",
		"-y", fullPath,
	)
	cmd := exec.CommandContext(jobCtx, "ffmpeg", args...)
	var stderr bytes.Buffer
	cmd.Stdout = os.Stdout
	cmd.Stderr = io.MultiWriter(os.Stderr, &stderr)
	cmd.Cancel = func() error {
		if cmd.Process == nil {
			return nil
		}
		return cmd.Process.Signal(os.Interrupt)
	}
	cmd.WaitDelay = 10 * time.Second
	logx.Infof("record ffmpeg started task_id=%d role=%s network_source=%t", taskID, task.Role, isNetworkSource(task.SourceURL))
	err = cmd.Run()
	if jobCtx.Err() != nil && !stopRequested.Load() {
		_ = client.RecordTask.UpdateOneID(taskID).SetStatus(recordtask.StatusCANCELED).SetErrorMessage(jobCtx.Err().Error()).Exec(ctx)
		logx.Warnf("record task canceled by context task_id=%d error=%v", taskID, jobCtx.Err())
		return jobCtx.Err()
	}
	if err != nil && !stopRequested.Load() {
		msg := commandError(err, stderr.String())
		if recovered, recoverErr := recoverPartialRecord(ctx, client, c.PostgresConf.DSN, taskID, task.Role, task.OutputPath, fullPath, msg); recovered {
			return nil
		} else if recoverErr != nil {
			msg = fmt.Sprintf("%s; partial output not usable: %v", msg, recoverErr)
		}
		markRecordFailed(ctx, client, c.PostgresConf.DSN, taskID, msg)
		return errors.New(msg)
	}

	if err := completeRecordOutput(ctx, client, c.PostgresConf.DSN, taskID, task.Role, task.OutputPath, fullPath); err != nil {
		markRecordFailed(ctx, client, c.PostgresConf.DSN, taskID, err.Error())
		return err
	}
	return nil
}

type recordPart struct {
	part int
	path string
}

func runMerge(ctx context.Context, client *ent.Client, c config.Config, taskID int) error {
	task, err := loadMergeTask(ctx, client, taskID)
	if err != nil {
		return errors.Wrap(err, "get merge task")
	}
	conf := c.RecordConf.WithDefaults()
	parts := mergeParts(task)
	if len(parts) == 0 {
		msg := fmt.Sprintf("no recorded parts found for role %s", task.Role)
		markRecordFailed(ctx, client, c.PostgresConf.DSN, taskID, msg)
		return errors.New(msg)
	}
	fullPath := storagepath.Resolve(conf.BaseDir, task.OutputPath)
	if err := os.MkdirAll(filepath.Dir(fullPath), 0o755); err != nil {
		return errors.Wrap(err, "create merge output dir")
	}
	if err := client.RecordTask.UpdateOneID(taskID).SetStatus(recordtask.StatusRUNNING).SetStartedAt(time.Now()).Exec(ctx); err != nil {
		return errors.Wrap(err, "mark merge running")
	}
	logx.Infof("record merge started task_id=%d role=%s parts=%d output=%s", taskID, task.Role, len(parts), path.Clean(task.OutputPath))
	if err := mergeFiles(conf.BaseDir, parts, fullPath); err != nil {
		markRecordFailed(ctx, client, c.PostgresConf.DSN, taskID, err.Error())
		return err
	}
	if err := completeRecordOutput(ctx, client, c.PostgresConf.DSN, taskID, task.Role, task.OutputPath, fullPath); err != nil {
		markRecordFailed(ctx, client, c.PostgresConf.DSN, taskID, err.Error())
		return err
	}
	logx.Infof("record merge completed task_id=%d role=%s parts=%d output=%s", taskID, task.Role, len(parts), path.Clean(task.OutputPath))
	return nil
}

func mergeParts(task *ent.RecordTask) []recordPart {
	round := task.Edges.MatchRound
	if round == nil || round.Edges.Match == nil {
		return nil
	}
	var parts []recordPart
	for _, r := range round.Edges.Match.Edges.Rounds {
		for _, t := range r.Edges.RecordTasks {
			base, part, ok := parsePartRole(t.Role)
			if !ok || base != task.Role || t.Status != recordtask.StatusSUCCEEDED {
				continue
			}
			for _, artifact := range t.Edges.MediaArtifacts {
				if artifact.Kind == mediaartifact.KindSource && artifact.Status == mediaartifact.StatusAVAILABLE {
					parts = append(parts, recordPart{part: part, path: artifact.Path})
					break
				}
			}
		}
	}
	sort.Slice(parts, func(i, j int) bool {
		return parts[i].part < parts[j].part
	})
	return parts
}

func mergeFiles(baseDir string, parts []recordPart, output string) error {
	tmp := fmt.Sprintf("%s.tmp.%d.%d", output, os.Getpid(), time.Now().UnixNano())
	if len(parts) == 1 {
		if err := copyFile(storagepath.Resolve(baseDir, parts[0].path), tmp); err != nil {
			_ = os.Remove(tmp)
			return errors.Wrap(err, "copy single record part")
		}
		return os.Rename(tmp, output)
	}
	listPath := fmt.Sprintf("%s.concat.%d.%d.txt", output, os.Getpid(), time.Now().UnixNano())
	var list bytes.Buffer
	for _, part := range parts {
		full := storagepath.Resolve(baseDir, part.path)
		list.WriteString("file '")
		list.WriteString(ffmpegConcatEscape(full))
		list.WriteString("'\n")
	}
	if err := os.WriteFile(listPath, list.Bytes(), 0o644); err != nil {
		return errors.Wrap(err, "write concat list")
	}
	defer os.Remove(listPath)
	cmd := exec.Command("ffmpeg", "-hide_banner", "-loglevel", "info", "-f", "concat", "-safe", "0", "-i", listPath, "-c", "copy", "-y", tmp)
	var stderr bytes.Buffer
	cmd.Stdout = os.Stdout
	cmd.Stderr = io.MultiWriter(os.Stderr, &stderr)
	if err := cmd.Run(); err != nil {
		_ = os.Remove(tmp)
		return errors.New(commandError(err, stderr.String()))
	}
	if err := os.Rename(tmp, output); err != nil {
		_ = os.Remove(tmp)
		return errors.Wrap(err, "rename merged output")
	}
	return nil
}

func copyFile(source, target string) error {
	src, err := os.Open(source)
	if err != nil {
		return errors.Wrap(err, "open source")
	}
	defer src.Close()
	dst, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return errors.Wrap(err, "create target")
	}
	_, copyErr := io.Copy(dst, src)
	syncErr := dst.Sync()
	closeErr := dst.Close()
	switch {
	case copyErr != nil:
		return errors.Wrap(copyErr, "copy file")
	case syncErr != nil:
		return errors.Wrap(syncErr, "sync target")
	case closeErr != nil:
		return errors.Wrap(closeErr, "close target")
	default:
		return nil
	}
}

func ffmpegConcatEscape(value string) string {
	return strings.ReplaceAll(value, "'", "'\\''")
}

func recoverPartialRecord(ctx context.Context, client *ent.Client, dsn string, taskID int, role, outputPath, fullPath, commandMsg string) (bool, error) {
	if isLocalOutputError(commandMsg) {
		return false, errors.New("ffmpeg reported a local output/storage error")
	}
	stat, err := os.Stat(fullPath)
	if err != nil {
		return false, errors.Wrap(err, "stat partial output")
	}
	if stat.Size() == 0 {
		return false, errors.New("partial output is empty")
	}
	if err := probeMedia(fullPath); err != nil {
		return false, err
	}
	logx.Warnf("record ffmpeg exited with error but partial output is kept task_id=%d role=%s size=%d output=%s error=%s", taskID, role, stat.Size(), path.Clean(outputPath), commandMsg)
	if err := completeRecordOutput(ctx, client, dsn, taskID, role, outputPath, fullPath); err != nil {
		return false, err
	}
	return true, nil
}

func isLocalOutputError(msg string) bool {
	lower := strings.ToLower(msg)
	for _, marker := range []string{
		"no space left on device",
		"permission denied",
		"read-only file system",
	} {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

func probeMedia(fullPath string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", fullPath)
	out, err := cmd.Output()
	if err != nil {
		return errors.Wrap(err, "probe partial output")
	}
	probed := string(out)
	if strings.Contains(probed, "video") || strings.Contains(probed, "audio") {
		return nil
	}
	return errors.New("partial output has no media stream")
}

func completeRecordOutput(ctx context.Context, client *ent.Client, dsn string, taskID int, role, outputPath, fullPath string) error {
	stat, statErr := os.Stat(fullPath)
	if statErr != nil {
		return errors.Wrap(statErr, "stat output")
	}
	sum, err := checksum(fullPath)
	if err != nil {
		return errors.Wrap(err, "checksum output")
	}
	if err := client.RecordTask.UpdateOneID(taskID).
		SetStatus(recordtask.StatusSUCCEEDED).
		SetCompletedAt(time.Now()).
		SetFileSize(stat.Size()).
		SetChecksum(sum).
		ClearErrorMessage().
		Exec(ctx); err != nil {
		return errors.Wrap(err, "mark record succeeded")
	}
	if err := upsertSourceArtifact(ctx, client, taskID, outputPath, stat.Size(), sum); err != nil {
		return errors.Wrap(err, "upsert source artifact")
	}
	logx.Infof("record task succeeded task_id=%d role=%s size=%d checksum=%s output=%s", taskID, role, stat.Size(), sum, path.Clean(outputPath))
	return db.Notify(ctx, dsn, db.RecordTaskChangedChannel, strconv.Itoa(taskID))
}

func markRecordFailed(ctx context.Context, client *ent.Client, dsn string, taskID int, msg string) {
	logx.Errorf("record task failed task_id=%d error=%s", taskID, msg)
	if err := client.RecordTask.UpdateOneID(taskID).SetStatus(recordtask.StatusFAILED).SetErrorMessage(msg).Exec(ctx); err != nil {
		logx.Errorf("mark record task %d failed: %v", taskID, err)
		return
	}
	if err := db.Notify(ctx, dsn, db.RecordTaskChangedChannel, strconv.Itoa(taskID)); err != nil {
		logx.Errorf("notify failed record task %d: %v", taskID, err)
	}
}

func isNetworkSource(source string) bool {
	lower := strings.ToLower(source)
	return strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://")
}

func upsertSourceArtifact(ctx context.Context, client *ent.Client, taskID int, outputPath string, size int64, sum string) error {
	return client.MediaArtifact.Create().
		SetRecordTaskID(taskID).
		SetKind(mediaartifact.KindSource).
		SetPath(outputPath).
		SetFormat(mediaartifact.FormatFlv).
		SetCodec(mediaartifact.CodecCopy).
		SetFileSize(size).
		SetChecksum(sum).
		SetStatus(mediaartifact.StatusAVAILABLE).
		OnConflictColumns(mediaartifact.RecordTaskColumn, mediaartifact.FieldKind).
		UpdateNewValues().
		Exec(ctx)
}

func commandError(err error, stderr string) string {
	const max = 2048
	msg := err.Error()
	if stderr != "" {
		if len(stderr) > max {
			stderr = stderr[len(stderr)-max:]
		}
		msg = fmt.Sprintf("%s: %s", msg, stderr)
	}
	return msg
}

func loadTask(ctx context.Context, client *ent.Client, taskID int) (*ent.RecordTask, error) {
	return client.RecordTask.Query().
		Where(recordtask.ID(taskID)).
		WithMatchRound(func(q *ent.MatchRoundQuery) {
			q.WithMatch(func(q *ent.MatchQuery) {
				q.WithRedTeam().WithBlueTeam().WithRounds(func(q *ent.MatchRoundQuery) {
					q.Order(matchround.ByRoundNo())
				})
			})
		}).
		Only(ctx)
}

func loadMergeTask(ctx context.Context, client *ent.Client, taskID int) (*ent.RecordTask, error) {
	return client.RecordTask.Query().
		Where(recordtask.ID(taskID)).
		WithMatchRound(func(q *ent.MatchRoundQuery) {
			q.WithMatch(func(q *ent.MatchQuery) {
				q.WithRedTeam().WithBlueTeam().WithRounds(func(q *ent.MatchRoundQuery) {
					q.Order(matchround.ByRoundNo()).
						WithRecordTasks(func(q *ent.RecordTaskQuery) {
							q.WithMediaArtifacts()
						})
				})
			})
		}).
		Only(ctx)
}

func parsePartRole(role string) (string, int, bool) {
	idx := strings.LastIndex(role, partRoleMarker)
	if idx < 0 {
		return role, 0, false
	}
	part, err := strconv.Atoi(role[idx+len(partRoleMarker):])
	if err != nil || part <= 0 {
		return role, 0, false
	}
	return role[:idx], part, true
}

func watchCancel(ctx context.Context, client *ent.Client, taskID int, stopRequested *atomic.Bool, cancel context.CancelFunc) {
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			task, err := client.RecordTask.Get(ctx, taskID)
			if err == nil && task.Status == recordtask.StatusCANCEL_REQUESTED {
				stopRequested.Store(true)
				logx.Warnf("record task stop requested task_id=%d", taskID)
				cancel()
				return
			}
		}
	}
}

func checksum(file string) (string, error) {
	f, err := os.Open(file)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
