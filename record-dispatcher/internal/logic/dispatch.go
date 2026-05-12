package logic

import (
	"context"
	"crypto/sha1"
	"encoding/hex"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/pkg/errors"
	"scutbot.cn/web/rm-monitor/ent"
	"scutbot.cn/web/rm-monitor/ent/match"
	"scutbot.cn/web/rm-monitor/ent/matchround"
	"scutbot.cn/web/rm-monitor/ent/recordtask"
	common "scutbot.cn/web/rm-monitor/pkg/config"
	"scutbot.cn/web/rm-monitor/pkg/db"
	"scutbot.cn/web/rm-monitor/pkg/kubejob"
	"scutbot.cn/web/rm-monitor/pkg/logx"
	"scutbot.cn/web/rm-monitor/pkg/pathfmt"
	"scutbot.cn/web/rm-monitor/pkg/recording"
	"scutbot.cn/web/rm-monitor/record-dispatcher/internal/svc"
)

type DispatchLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

const dispatchingStaleAfter = 5 * time.Minute
const manifestLookback = 30 * time.Second
const matchStatusStarted = "STARTED"

func NewDispatchLogic(ctx context.Context, svcCtx *svc.ServiceContext) *DispatchLogic {
	return &DispatchLogic{ctx: ctx, svcCtx: svcCtx, Logger: logx.WithContext(ctx)}
}

func (l *DispatchLogic) Tick() error {
	if err := l.cancelEndedRounds(); err != nil {
		return err
	}
	if err := l.createTasksForStartedRounds(); err != nil {
		return err
	}
	if err := l.recoverDispatchingTasks(); err != nil {
		return err
	}
	if err := l.dispatchPendingTasks(); err != nil {
		return err
	}
	return l.dispatchRecentManifestJobs()
}

func (l *DispatchLogic) cancelEndedRounds() error {
	tasks, err := l.svcCtx.DB.RecordTask.Query().
		Where(recordtask.StatusIn(recordtask.StatusRUNNING, recordtask.StatusDISPATCHING)).
		WithMatchRound(func(q *ent.MatchRoundQuery) {
			q.WithMatch()
		}).
		Limit(200).
		All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query running record tasks")
	}
	for _, task := range tasks {
		if shouldCancelRecordTask(task) {
			if err := l.svcCtx.DB.RecordTask.UpdateOneID(task.ID).SetStatus(recordtask.StatusCANCEL_REQUESTED).Exec(l.ctx); err != nil {
				return errors.Wrap(err, "request record cancel")
			}
			_ = db.Notify(l.ctx, l.svcCtx.Config.PostgresConf.DSN, db.RecordTaskChangedChannel, strconv.Itoa(task.ID))
		}
	}
	return nil
}

func shouldCancelRecordTask(task *ent.RecordTask) bool {
	round := task.Edges.MatchRound
	if round == nil {
		return false
	}
	if round.Edges.Match == nil {
		return round.Status == matchround.StatusENDED
	}
	return round.Edges.Match.LatestStatus != matchStatusStarted
}

func (l *DispatchLogic) recoverDispatchingTasks() error {
	if l.svcCtx.K8s == nil {
		return nil
	}
	tasks, err := l.svcCtx.DB.RecordTask.Query().
		Where(recordtask.StatusEQ(recordtask.StatusDISPATCHING), recordtask.UpdatedAtLTE(time.Now().Add(-dispatchingStaleAfter))).
		Limit(100).
		All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query stale dispatching record tasks")
	}
	namespace := l.svcCtx.Config.K8sJobConf.WithDefaults().Namespace
	for _, task := range tasks {
		name := jobName("record", task.ID)
		if task.K8sJobName != nil && *task.K8sJobName != "" {
			name = *task.K8sJobName
		}
		exists, err := l.svcCtx.K8s.JobExists(l.ctx, namespace, name)
		if err != nil {
			return err
		}
		if exists {
			if err := l.svcCtx.DB.RecordTask.UpdateOneID(task.ID).SetStatus(recordtask.StatusRUNNING).SetStartedAt(time.Now()).Exec(l.ctx); err != nil {
				return errors.Wrap(err, "recover running record task")
			}
			l.Warnf("record task recovered task_id=%d job=%s status=RUNNING", task.ID, name)
			continue
		}
		if err := l.svcCtx.DB.RecordTask.UpdateOneID(task.ID).SetStatus(recordtask.StatusPENDING).Exec(l.ctx); err != nil {
			return errors.Wrap(err, "requeue stale record task")
		}
		l.Warnf("record task requeued task_id=%d missing_job=%s", task.ID, name)
	}
	return nil
}

func (l *DispatchLogic) createTasksForStartedRounds() error {
	rounds, err := l.svcCtx.DB.MatchRound.Query().
		Where(matchround.StatusEQ(matchround.StatusSTARTED)).
		WithMatch(func(q *ent.MatchQuery) { q.WithRedTeam().WithBlueTeam() }).
		Limit(100).
		All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query started rounds")
	}
	conf := l.svcCtx.Config.RecordConf.WithDefaults()
	handledMatches := map[string]struct{}{}
	for _, r := range rounds {
		m := r.Edges.Match
		if m == nil {
			continue
		}
		if _, ok := handledMatches[m.ID]; ok {
			continue
		}
		handledMatches[m.ID] = struct{}{}
		urls, err := recording.LiveURLs(l.ctx, l.svcCtx.RestyClient, conf.LiveInfoURL, m.Zone, conf.Res)
		if err != nil {
			l.Errorf("live urls for match %s: %v", m.ID, err)
			continue
		}
		l.Infof("live urls fetched match_id=%s zone=%s order=%d res=%s roles=%d", m.ID, m.Zone, m.Order, conf.Res, len(urls))
		existingRoles, err := l.recordRolesForMatch(m.ID)
		if err != nil {
			return err
		}
		created := 0
		for role, url := range urls {
			if existingRoles[role] {
				continue
			}
			output, err := l.outputPath(conf, m, 1, role)
			if err != nil {
				return err
			}
			err = l.svcCtx.DB.RecordTask.Create().
				SetMatchRoundID(r.ID).
				SetRole(role).
				SetSourceURL(url).
				SetOutputPath(output).
				SetStatus(recordtask.StatusPENDING).
				OnConflictColumns(recordtask.MatchRoundColumn, recordtask.FieldRole).
				DoNothing().
				Exec(l.ctx)
			if err != nil {
				if db.IsNoRows(err) {
					continue
				}
				return errors.Wrap(err, "create record task")
			}
			created++
			l.Infof("record task created match_id=%s zone=%s order=%d role=%s output=%s", m.ID, m.Zone, m.Order, role, output)
		}
		if created == 0 && len(existingRoles) > 0 {
			l.Debugf("record tasks already exist match_id=%s zone=%s order=%d roles=%d", m.ID, m.Zone, m.Order, len(existingRoles))
		}
	}
	return nil
}

func (l *DispatchLogic) recordRolesForMatch(matchID string) (map[string]bool, error) {
	tasks, err := l.svcCtx.DB.RecordTask.Query().
		Where(recordtask.HasMatchRoundWith(matchround.HasMatchWith(match.ID(matchID)))).
		Limit(200).
		All(l.ctx)
	if err != nil {
		return nil, errors.Wrap(err, "query existing record tasks for match")
	}
	out := make(map[string]bool, len(tasks))
	for _, task := range tasks {
		out[task.Role] = true
	}
	return out, nil
}

func (l *DispatchLogic) outputPath(conf common.RecordConf, m *ent.Match, roundNo int, role string) (string, error) {
	red, err := m.Edges.RedTeamOrErr()
	if err != nil {
		return "", err
	}
	blue, err := m.Edges.BlueTeamOrErr()
	if err != nil {
		return "", err
	}
	return pathfmt.Render(conf.MatchNameTemplate, conf.PathTemplate, pathfmt.Data{
		Event:      m.Event,
		Zone:       m.Zone,
		Order:      m.Order,
		RedSchool:  red.SchoolName,
		RedName:    red.Name,
		BlueSchool: blue.SchoolName,
		BlueName:   blue.Name,
		RoundNo:    roundNo,
		Role:       role,
	})
}

func (l *DispatchLogic) dispatchPendingTasks() error {
	tasks, err := l.svcCtx.DB.RecordTask.Query().
		Where(recordtask.StatusEQ(recordtask.StatusPENDING)).
		Limit(20).
		All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query pending record tasks")
	}
	for _, task := range tasks {
		jobName := jobName("record", task.ID)
		claimed, err := l.svcCtx.DB.RecordTask.Update().
			Where(recordtask.ID(task.ID), recordtask.StatusEQ(recordtask.StatusPENDING)).
			SetStatus(recordtask.StatusDISPATCHING).
			AddAttempts(1).
			SetK8sJobName(jobName).
			Save(l.ctx)
		if err != nil {
			return errors.Wrap(err, "mark record dispatching")
		}
		if claimed == 0 {
			continue
		}
		l.Infof("record task dispatching task_id=%d job=%s", task.ID, jobName)
		if l.svcCtx.K8s != nil {
			job := kubejob.Build(l.svcCtx.Config.K8sJobConf, kubejob.JobSpec{
				Name:     jobName,
				App:      "record-job",
				Image:    l.svcCtx.Config.K8sJobConf.Image,
				Args:     []string{"-f", "/etc/rm-monitor/config.yml", "-task", strconv.Itoa(task.ID)},
				MountPVC: true,
				CPU:      "500m",
				Memory:   "512Mi",
			})
			if err := l.svcCtx.K8s.CreateJob(l.ctx, l.svcCtx.Config.K8sJobConf.WithDefaults().Namespace, job); err != nil {
				_ = l.svcCtx.DB.RecordTask.UpdateOneID(task.ID).SetStatus(recordtask.StatusFAILED).SetErrorMessage(err.Error()).Exec(l.ctx)
				l.Errorf("record job create failed task_id=%d job=%s error=%v", task.ID, jobName, err)
				return err
			}
			l.Infof("record job created task_id=%d job=%s", task.ID, jobName)
		}
		if err := l.svcCtx.DB.RecordTask.UpdateOneID(task.ID).SetStatus(recordtask.StatusRUNNING).SetStartedAt(time.Now()).Exec(l.ctx); err != nil {
			return errors.Wrap(err, "mark record running")
		}
		l.Infof("record task running task_id=%d job=%s", task.ID, jobName)
		_ = db.Notify(l.ctx, l.svcCtx.Config.PostgresConf.DSN, db.RecordTaskChangedChannel, strconv.Itoa(task.ID))
	}
	return nil
}

func (l *DispatchLogic) dispatchRecentManifestJobs() error {
	if l.svcCtx.K8s == nil || strings.TrimSpace(l.svcCtx.Config.ManifestJobConf.Image) == "" {
		return nil
	}
	since := time.Now().Add(-manifestLookback)
	type manifestCandidate struct {
		match     *ent.Match
		updatedAt time.Time
	}
	matchesByID := map[string]manifestCandidate{}
	rounds, err := l.svcCtx.DB.MatchRound.Query().
		Where(matchround.UpdatedAtGTE(since)).
		WithMatch().
		Limit(200).
		All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query recently changed rounds for manifest")
	}
	for _, r := range rounds {
		if r.Edges.Match != nil {
			cur := matchesByID[r.Edges.Match.ID]
			if cur.match == nil || r.UpdatedAt.After(cur.updatedAt) {
				matchesByID[r.Edges.Match.ID] = manifestCandidate{match: r.Edges.Match, updatedAt: r.UpdatedAt}
			}
		}
	}
	conf := l.svcCtx.Config.ManifestJobConf.WithDefaults()
	for _, item := range matchesByID {
		m := item.match
		name := manifestJobName(m.ID, item.updatedAt)
		job := kubejob.Build(l.svcCtx.Config.ManifestJobConf, kubejob.JobSpec{
			Name:     name,
			App:      "manifest-job",
			Image:    conf.Image,
			Args:     []string{"-f", "/etc/rm-monitor/config.yml", "-match", m.ID},
			MountPVC: true,
			CPU:      "50m",
			Memory:   "128Mi",
		})
		if err := l.svcCtx.K8s.CreateJob(l.ctx, conf.Namespace, job); err != nil {
			return errors.Wrap(err, "create manifest job")
		}
	}
	return nil
}

func jobName(prefix string, id int) string {
	return strings.ToLower(fmt.Sprintf("%s-%d", prefix, id))
}

func manifestJobName(matchID string, updatedAt time.Time) string {
	h := sha1.Sum([]byte(fmt.Sprintf("%s:%d", matchID, updatedAt.UnixNano())))
	return "manifest-" + hex.EncodeToString(h[:])[:16]
}
