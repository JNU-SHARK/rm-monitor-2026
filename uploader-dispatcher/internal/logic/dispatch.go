package logic

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	larkbitable "github.com/larksuite/oapi-sdk-go/v3/service/bitable/v1"
	larkim "github.com/larksuite/oapi-sdk-go/v3/service/im/v1"
	"github.com/pkg/errors"
	"scutbot.cn/web/rm-monitor/ent"
	"scutbot.cn/web/rm-monitor/ent/mediaartifact"
	"scutbot.cn/web/rm-monitor/ent/uploadtask"
	"scutbot.cn/web/rm-monitor/pkg/bitableupload"
	common "scutbot.cn/web/rm-monitor/pkg/config"
	"scutbot.cn/web/rm-monitor/pkg/db"
	"scutbot.cn/web/rm-monitor/pkg/kubejob"
	"scutbot.cn/web/rm-monitor/pkg/logx"
	"scutbot.cn/web/rm-monitor/pkg/storagepath"
	"scutbot.cn/web/rm-monitor/uploader-dispatcher/internal/svc"
)

type DispatchLogic struct {
	ctx    context.Context
	svcCtx *svc.ServiceContext
	logx.Logger
}

const dispatchingStaleAfter = 5 * time.Minute
const tableCacheTTL = 24 * 3600
const tableLockTTL = 30
const partRoleMarker = "__part"

type bitableFieldSpec struct {
	name      string
	fieldType int
}

func NewDispatchLogic(ctx context.Context, svcCtx *svc.ServiceContext) *DispatchLogic {
	return &DispatchLogic{ctx: ctx, svcCtx: svcCtx, Logger: logx.WithContext(ctx)}
}

func (l *DispatchLogic) Tick() error {
	if err := l.createUploadTasks(); err != nil {
		return err
	}
	if err := l.recoverDispatching(); err != nil {
		return err
	}
	return l.dispatchPending()
}

func (l *DispatchLogic) createUploadTasks() error {
	conf := l.svcCtx.Config.UploadConf.WithDefaults()
	if strings.TrimSpace(conf.BitableAppToken) == "" {
		return errors.New("UploadConf.BitableAppToken is required")
	}
	artifactKind, err := uploadArtifactKind(conf)
	if err != nil {
		return err
	}
	artifacts, err := l.svcCtx.DB.MediaArtifact.Query().
		Where(
			mediaartifact.KindEQ(artifactKind),
			mediaartifact.StatusEQ(mediaartifact.StatusAVAILABLE),
			mediaartifact.HasRecordTask(),
			mediaartifact.Not(mediaartifact.HasUploadTask()),
		).
		WithRecordTask(func(q *ent.RecordTaskQuery) {
			q.WithMediaArtifacts()
			q.WithMatchRound(func(q *ent.MatchRoundQuery) {
				q.WithMatch(func(q *ent.MatchQuery) {
					q.WithRedTeam().WithBlueTeam()
				})
			})
		}).
		Limit(100).
		All(l.ctx)
	if err != nil {
		return errors.Wrapf(err, "query %s artifacts", artifactKind)
	}
	for _, artifact := range artifacts {
		recordTask := artifact.Edges.RecordTask
		if recordTask == nil || recordTask.Edges.MatchRound == nil || recordTask.Edges.MatchRound.Edges.Match == nil {
			continue
		}
		if isPartRole(recordTask.Role) {
			continue
		}
		match := recordTask.Edges.MatchRound.Edges.Match
		tableID, err := l.ensureTable(conf.BitableAppToken, bitableupload.TableName(match.Event, match.Zone), !conf.DisableFileUpload)
		if err != nil {
			return err
		}
		relativePath, err := relativeArtifactPath(artifact.Path)
		if err != nil {
			return err
		}
		recordID, recordURL, err := l.createBitableRecord(conf.BitableAppToken, tableID, artifact.ID, match, recordTask.Role, relativePath)
		if err != nil {
			return err
		}
		l.Infof("bitable record created artifact_id=%d match_id=%s zone=%s order=%d role=%s table=%s record=%s path=%s", artifact.ID, match.ID, match.Zone, match.Order, recordTask.Role, tableID, recordID, relativePath)
		copyErr := l.artifactLongTermCopy(conf, artifact, relativePath)
		needsLocalDelete := copyErr == nil && conf.DisableFileUpload && conf.LongTermBaseDir != "" && conf.DeleteLocalAfterCopy
		create := l.svcCtx.DB.UploadTask.Create().
			SetRecordTaskID(recordTask.ID).
			SetSourceArtifactID(artifact.ID).
			SetSourcePath(artifact.Path).
			SetBitableAppToken(conf.BitableAppToken).
			SetBitableTableID(tableID).
			SetBitableRecordID(recordID).
			SetNillableBitableRecordURL(recordURL)
		switch {
		case copyErr != nil:
			create.SetStatus(uploadtask.StatusFAILED).SetErrorMessage(copyErr.Error())
		case needsLocalDelete:
			create.SetStatus(uploadtask.StatusRUNNING)
		case conf.DisableFileUpload:
			create.SetStatus(uploadtask.StatusSUCCEEDED).SetCompletedAt(time.Now())
		default:
			create.SetStatus(uploadtask.StatusPENDING)
		}
		id, err := create.
			OnConflictColumns(uploadtask.SourceArtifactColumn).
			DoNothing().
			ID(l.ctx)
		if err != nil {
			if db.IsNoRows(err) {
				continue
			}
			return errors.Wrap(err, "create upload task")
		}
		if copyErr != nil {
			l.Errorf("artifact long-term copy failed: %v", copyErr)
			_ = l.notifyCopyFailure(conf, relativePath, copyErr)
			continue
		}
		if conf.LongTermBaseDir != "" {
			l.Infof("artifact long-term copy succeeded path=%s target_base=%s", relativePath, conf.LongTermBaseDir)
		}
		if needsLocalDelete {
			if err := l.deleteLocalArtifacts(conf.BaseDir, artifact); err != nil {
				deleteErr := errors.Wrapf(err, "delete local artifacts after long-term copy %s", relativePath)
				_ = l.svcCtx.DB.UploadTask.UpdateOneID(id).
					SetStatus(uploadtask.StatusFAILED).
					SetErrorMessage(deleteErr.Error()).
					Exec(l.ctx)
				l.Errorf("archive long-term cleanup failed: %v", deleteErr)
				_ = l.notifyCopyFailure(conf, relativePath, deleteErr)
				continue
			}
			l.Infof("local artifacts deleted after copy path=%s", relativePath)
			if err := l.svcCtx.DB.UploadTask.UpdateOneID(id).
				SetStatus(uploadtask.StatusSUCCEEDED).
				SetCompletedAt(time.Now()).
				Exec(l.ctx); err != nil {
				return errors.Wrap(err, "mark artifact copy succeeded")
			}
		}
		if conf.DisableFileUpload {
			l.Infof("upload task completed without file upload task_id=%d artifact_id=%d path=%s", id, artifact.ID, relativePath)
			_ = db.Notify(l.ctx, l.svcCtx.Config.PostgresConf.DSN, db.UploadTaskChangedChannel, strconv.Itoa(id))
		}
	}
	return nil
}

func uploadArtifactKind(conf common.UploadConf) (mediaartifact.Kind, error) {
	kind := mediaartifact.Kind(strings.TrimSpace(conf.ArtifactKind))
	if kind == "" {
		return mediaartifact.KindArchive, nil
	}
	if err := mediaartifact.KindValidator(kind); err != nil {
		return "", err
	}
	return kind, nil
}

func (l *DispatchLogic) artifactLongTermCopy(conf common.UploadConf, artifact *ent.MediaArtifact, relativePath string) error {
	if strings.TrimSpace(conf.LongTermBaseDir) == "" {
		return nil
	}
	sourcePath := storagepath.Resolve(conf.BaseDir, relativePath)
	targetPath := storagepath.Resolve(conf.LongTermBaseDir, relativePath)
	if err := copyAndVerify(sourcePath, targetPath, artifact.Checksum); err != nil {
		return errors.Wrapf(err, "copy %s to long-term storage", relativePath)
	}
	return nil
}

func copyAndVerify(sourcePath, targetPath string, expectedChecksum *string) error {
	sourceInfo, err := os.Stat(sourcePath)
	if err != nil {
		return errors.Wrap(err, "stat source")
	}
	if sourceInfo.IsDir() {
		return errors.Errorf("source is a directory: %s", sourcePath)
	}
	checksum := ""
	if expectedChecksum != nil && *expectedChecksum != "" {
		checksum = *expectedChecksum
	} else {
		checksum, err = fileChecksum(sourcePath)
		if err != nil {
			return errors.Wrap(err, "checksum source")
		}
	}
	if err := verifyCopiedFile(targetPath, sourceInfo.Size(), checksum); err == nil {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(targetPath), 0o755); err != nil {
		return errors.Wrap(err, "create long-term dir")
	}
	tmpPath := fmt.Sprintf("%s.tmp.%d.%d", targetPath, os.Getpid(), time.Now().UnixNano())
	if err := copyFile(sourcePath, tmpPath); err != nil {
		_ = os.Remove(tmpPath)
		return err
	}
	if err := os.Rename(tmpPath, targetPath); err != nil {
		_ = os.Remove(tmpPath)
		return errors.Wrap(err, "rename copied file")
	}
	return verifyCopiedFile(targetPath, sourceInfo.Size(), checksum)
}

func copyFile(sourcePath, targetPath string) error {
	source, err := os.Open(sourcePath)
	if err != nil {
		return errors.Wrap(err, "open source")
	}
	defer source.Close()
	target, err := os.OpenFile(targetPath, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o644)
	if err != nil {
		return errors.Wrap(err, "create target")
	}
	_, copyErr := io.Copy(target, source)
	syncErr := target.Sync()
	closeErr := target.Close()
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

func verifyCopiedFile(filePath string, expectedSize int64, expectedChecksum string) error {
	info, err := os.Stat(filePath)
	if err != nil {
		return errors.Wrap(err, "stat copied file")
	}
	if info.Size() != expectedSize {
		return errors.Errorf("copied file size mismatch: got %d want %d", info.Size(), expectedSize)
	}
	if expectedChecksum == "" {
		return nil
	}
	checksum, err := fileChecksum(filePath)
	if err != nil {
		return errors.Wrap(err, "checksum copied file")
	}
	if checksum != expectedChecksum {
		return errors.Errorf("copied file checksum mismatch: got %s want %s", checksum, expectedChecksum)
	}
	return nil
}

func fileChecksum(filePath string) (string, error) {
	f, err := os.Open(filePath)
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

func isPartRole(role string) bool {
	idx := strings.LastIndex(role, partRoleMarker)
	if idx < 0 {
		return false
	}
	_, err := strconv.Atoi(role[idx+len(partRoleMarker):])
	return err == nil
}

func (l *DispatchLogic) deleteLocalArtifacts(baseDir string, archive *ent.MediaArtifact) error {
	now := time.Now()
	artifacts := []*ent.MediaArtifact{archive}
	if archive.Edges.RecordTask != nil {
		for _, artifact := range archive.Edges.RecordTask.Edges.MediaArtifacts {
			if artifact.ID == archive.ID || artifact.Status != mediaartifact.StatusAVAILABLE {
				continue
			}
			if artifact.Kind == mediaartifact.KindSource || artifact.Kind == mediaartifact.KindArchive {
				artifacts = append(artifacts, artifact)
			}
		}
	}
	seen := make(map[int]struct{}, len(artifacts))
	for _, artifact := range artifacts {
		if _, ok := seen[artifact.ID]; ok {
			continue
		}
		seen[artifact.ID] = struct{}{}
		fullPath := storagepath.Resolve(baseDir, artifact.Path)
		if err := os.Remove(fullPath); err != nil && !os.IsNotExist(err) {
			return errors.Wrapf(err, "remove local artifact %s", artifact.Path)
		}
		if err := l.svcCtx.DB.MediaArtifact.UpdateOneID(artifact.ID).
			SetStatus(mediaartifact.StatusDELETED).
			SetDeletedAt(now).
			Exec(l.ctx); err != nil {
			return errors.Wrapf(err, "mark local artifact deleted %s", artifact.Path)
		}
	}
	return nil
}

func (l *DispatchLogic) ensureTable(appToken, tableName string, includeAttachment bool) (string, error) {
	cacheKey := fmt.Sprintf("rm-monitor:bitable:table:%s:%s", appToken, tableName)
	if tableID, err := l.svcCtx.Redis.GetCtx(l.ctx, cacheKey); err != nil {
		return "", errors.Wrap(err, "get bitable table cache")
	} else if tableID != "" {
		if err := l.ensureTableFields(appToken, tableID, includeAttachment); err != nil {
			return "", err
		}
		return tableID, nil
	}
	if tableID, err := l.findTable(appToken, tableName); err != nil {
		return "", err
	} else if tableID != "" {
		if err := l.ensureTableFields(appToken, tableID, includeAttachment); err != nil {
			return "", err
		}
		_ = l.svcCtx.Redis.SetexCtx(l.ctx, cacheKey, tableID, tableCacheTTL)
		return tableID, nil
	}
	lockKey := cacheKey + ":lock"
	locked, err := l.svcCtx.Redis.SetNXCtx(l.ctx, lockKey, "1", tableLockTTL)
	if err != nil {
		return "", errors.Wrap(err, "lock bitable table creation")
	}
	if !locked {
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			time.Sleep(500 * time.Millisecond)
			if tableID, err := l.svcCtx.Redis.GetCtx(l.ctx, cacheKey); err != nil {
				return "", errors.Wrap(err, "get bitable table cache")
			} else if tableID != "" {
				if err := l.ensureTableFields(appToken, tableID, includeAttachment); err != nil {
					return "", err
				}
				return tableID, nil
			}
			if tableID, err := l.findTable(appToken, tableName); err != nil {
				return "", err
			} else if tableID != "" {
				if err := l.ensureTableFields(appToken, tableID, includeAttachment); err != nil {
					return "", err
				}
				_ = l.svcCtx.Redis.SetexCtx(l.ctx, cacheKey, tableID, tableCacheTTL)
				return tableID, nil
			}
		}
		return "", errors.Errorf("wait bitable table creation timeout: %s", tableName)
	}
	defer func() { _ = l.svcCtx.Redis.DelCtx(context.Background(), lockKey) }()
	if tableID, err := l.findTable(appToken, tableName); err != nil {
		return "", err
	} else if tableID != "" {
		if err := l.ensureTableFields(appToken, tableID, includeAttachment); err != nil {
			return "", err
		}
		_ = l.svcCtx.Redis.SetexCtx(l.ctx, cacheKey, tableID, tableCacheTTL)
		return tableID, nil
	}
	resp, err := l.svcCtx.Lark.Bitable.V1.AppTable.Create(l.ctx, larkbitable.NewCreateAppTableReqBuilder().
		AppToken(appToken).
		Body(larkbitable.NewCreateAppTableReqBodyBuilder().
			Table(larkbitable.NewReqTableBuilder().
				Name(tableName).
				DefaultViewName("表格").
				Fields(bitableCreateHeaders(includeAttachment)).
				Build()).
			Build()).
		Build())
	if err != nil {
		return "", errors.Wrap(err, "create bitable table")
	}
	if !resp.Success() || resp.Data == nil || resp.Data.TableId == nil {
		return "", errors.Wrap(resp, "create bitable table")
	}
	if err := l.ensureTableFields(appToken, *resp.Data.TableId, includeAttachment); err != nil {
		return "", err
	}
	_ = l.svcCtx.Redis.SetexCtx(l.ctx, cacheKey, *resp.Data.TableId, tableCacheTTL)
	return *resp.Data.TableId, nil
}

func (l *DispatchLogic) findTable(appToken, tableName string) (string, error) {
	pageToken := ""
	for {
		builder := larkbitable.NewListAppTableReqBuilder().
			AppToken(appToken).
			PageSize(100)
		if pageToken != "" {
			builder.PageToken(pageToken)
		}
		resp, err := l.svcCtx.Lark.Bitable.V1.AppTable.List(l.ctx, builder.Build())
		if err != nil {
			return "", errors.Wrap(err, "list bitable tables")
		}
		if !resp.Success() {
			return "", errors.Errorf("list bitable tables: code=%d msg=%s", resp.Code, resp.Msg)
		}
		if resp.Data == nil {
			return "", nil
		}
		for _, table := range resp.Data.Items {
			if table.Name != nil && table.TableId != nil && *table.Name == tableName {
				return *table.TableId, nil
			}
		}
		if resp.Data.HasMore == nil || !*resp.Data.HasMore || resp.Data.PageToken == nil || *resp.Data.PageToken == "" {
			return "", nil
		}
		pageToken = *resp.Data.PageToken
	}
}

func (l *DispatchLogic) ensureTableFields(appToken, tableID string, includeAttachment bool) error {
	existing, err := l.listFields(appToken, tableID)
	if err != nil {
		return err
	}
	for _, field := range bitableFields(includeAttachment) {
		existingType, ok := existing[field.name]
		if ok {
			if existingType != field.fieldType {
				return errors.Errorf("bitable field %s has type %d, expected %d", field.name, existingType, field.fieldType)
			}
			continue
		}
		resp, err := l.svcCtx.Lark.Bitable.V1.AppTableField.Create(l.ctx, larkbitable.NewCreateAppTableFieldReqBuilder().
			AppToken(appToken).
			TableId(tableID).
			AppTableField(larkbitable.NewAppTableFieldBuilder().
				FieldName(field.name).
				Type(field.fieldType).
				Build()).
			Build())
		if err != nil {
			return errors.Wrapf(err, "create bitable field %s", field.name)
		}
		if !resp.Success() {
			return errors.Errorf("create bitable field %s: code=%d msg=%s", field.name, resp.Code, resp.Msg)
		}
	}
	return nil
}

func bitableFields(includeAttachment bool) []bitableFieldSpec {
	fields := []bitableFieldSpec{
		{name: bitableupload.FieldMatch, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldStage, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldRedTeam, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldBlueTeam, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldRole, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldFilePath, fieldType: larkbitable.TypeText},
		{name: bitableupload.FieldBilibili, fieldType: larkbitable.TypeUrl},
	}
	if includeAttachment {
		fields = append(fields, bitableFieldSpec{name: bitableupload.FieldAttachment, fieldType: larkbitable.TypeAttachment})
	}
	return fields
}

func bitableCreateHeaders(includeAttachment bool) []*larkbitable.AppTableCreateHeader {
	fields := bitableFields(includeAttachment)
	headers := make([]*larkbitable.AppTableCreateHeader, 0, len(fields))
	for _, field := range fields {
		headers = append(headers, larkbitable.NewAppTableCreateHeaderBuilder().
			FieldName(field.name).
			Type(field.fieldType).
			Build())
	}
	return headers
}

func (l *DispatchLogic) listFields(appToken, tableID string) (map[string]int, error) {
	fields := make(map[string]int)
	pageToken := ""
	for {
		builder := larkbitable.NewListAppTableFieldReqBuilder().
			AppToken(appToken).
			TableId(tableID).
			PageSize(100)
		if pageToken != "" {
			builder.PageToken(pageToken)
		}
		resp, err := l.svcCtx.Lark.Bitable.V1.AppTableField.List(l.ctx, builder.Build())
		if err != nil {
			return nil, errors.Wrap(err, "list bitable fields")
		}
		if !resp.Success() {
			return nil, errors.Errorf("list bitable fields: code=%d msg=%s", resp.Code, resp.Msg)
		}
		if resp.Data == nil {
			return fields, nil
		}
		for _, field := range resp.Data.Items {
			if field.FieldName == nil || field.Type == nil {
				continue
			}
			fields[*field.FieldName] = *field.Type
		}
		if resp.Data.HasMore == nil || !*resp.Data.HasMore || resp.Data.PageToken == nil || *resp.Data.PageToken == "" {
			return fields, nil
		}
		pageToken = *resp.Data.PageToken
	}
}

func (l *DispatchLogic) createBitableRecord(appToken, tableID string, _ int, match *ent.Match, role, filePath string) (string, *string, error) {
	resp, err := l.svcCtx.Lark.Bitable.V1.AppTableRecord.Create(l.ctx, larkbitable.NewCreateAppTableRecordReqBuilder().
		AppToken(appToken).
		TableId(tableID).
		AppTableRecord(larkbitable.NewAppTableRecordBuilder().
			Fields(bitableupload.RecordFieldsWithPath(match, role, filePath)).
			Build()).
		Build())
	if err != nil {
		return "", nil, errors.Wrap(err, "create bitable record")
	}
	if !resp.Success() || resp.Data == nil || resp.Data.Record == nil || resp.Data.Record.RecordId == nil {
		return "", nil, errors.Wrap(resp, "create bitable record")
	}
	record := resp.Data.Record
	if record.RecordUrl != nil && *record.RecordUrl != "" {
		return *record.RecordId, record.RecordUrl, nil
	}
	if record.SharedUrl != nil && *record.SharedUrl != "" {
		return *record.RecordId, record.SharedUrl, nil
	}
	url := fmt.Sprintf("https://scutrobotlab.feishu.cn/base/%s?table=%s&record=%s", appToken, tableID, *record.RecordId)
	return *record.RecordId, &url, nil
}

func relativeArtifactPath(p string) (string, error) {
	rel := path.Clean(filepath.ToSlash(strings.TrimSpace(p)))
	if rel == "." || rel == "" {
		return "", errors.New("empty artifact path")
	}
	if strings.HasPrefix(rel, "/") || rel == ".." || strings.HasPrefix(rel, "../") {
		return "", errors.Errorf("artifact path escapes records dir: %s", p)
	}
	return rel, nil
}

func (l *DispatchLogic) notifyCopyFailure(conf common.UploadConf, relativePath string, copyErr error) error {
	chatIDs, err := l.joinedChatIDs()
	if err != nil {
		return err
	}
	for _, chatID := range chatIDs {
		mentionID := strings.TrimSpace(conf.CopyFailureMentionOpenID)
		if mentionID == "" && strings.TrimSpace(conf.CopyFailureMentionName) != "" {
			if id, err := l.findChatMemberOpenID(chatID, conf.CopyFailureMentionName); err == nil {
				mentionID = id
			} else {
				l.Errorf("find lark mention member %s failed: %v", conf.CopyFailureMentionName, err)
			}
		}
		content, err := copyFailureContent(conf, relativePath, copyErr, mentionID)
		if err != nil {
			return err
		}
		resp, err := l.svcCtx.Lark.Im.V1.Message.Create(l.ctx, larkim.NewCreateMessageReqBuilder().
			ReceiveIdType(larkim.ReceiveIdTypeChatId).
			Body(larkim.NewCreateMessageReqBodyBuilder().
				ReceiveId(chatID).
				MsgType(larkim.MsgTypePost).
				Content(content).
				Build()).
			Build())
		if err != nil {
			return errors.Wrap(err, "send copy failure message")
		}
		if !resp.Success() {
			return errors.Wrap(resp, "send copy failure message")
		}
	}
	return nil
}

func (l *DispatchLogic) joinedChatIDs() ([]string, error) {
	pageToken := ""
	var ids []string
	for {
		builder := larkim.NewListChatReqBuilder().PageSize(20)
		if pageToken != "" {
			builder.PageToken(pageToken)
		}
		resp, err := l.svcCtx.Lark.Im.V1.Chat.List(l.ctx, builder.Build())
		if err != nil {
			return nil, errors.Wrap(err, "list lark chats")
		}
		if !resp.Success() {
			return nil, errors.Wrap(resp, "list lark chats")
		}
		if resp.Data != nil {
			for _, chat := range resp.Data.Items {
				if chat.ChatId != nil && *chat.ChatId != "" {
					ids = append(ids, *chat.ChatId)
				}
			}
			if resp.Data.HasMore != nil && *resp.Data.HasMore && resp.Data.PageToken != nil && *resp.Data.PageToken != "" {
				pageToken = *resp.Data.PageToken
				continue
			}
		}
		return ids, nil
	}
}

func (l *DispatchLogic) findChatMemberOpenID(chatID, name string) (string, error) {
	targetName := strings.TrimSpace(name)
	if targetName == "" {
		return "", errors.New("empty mention name")
	}
	pageToken := ""
	for {
		builder := larkim.NewGetChatMembersReqBuilder().
			ChatId(chatID).
			MemberIdType(larkim.MemberIdTypeGetChatMembersOpenId).
			PageSize(50)
		if pageToken != "" {
			builder.PageToken(pageToken)
		}
		resp, err := l.svcCtx.Lark.Im.V1.ChatMembers.Get(l.ctx, builder.Build())
		if err != nil {
			return "", errors.Wrap(err, "list lark chat members")
		}
		if !resp.Success() {
			return "", errors.Wrap(resp, "list lark chat members")
		}
		if resp.Data != nil {
			for _, member := range resp.Data.Items {
				if member.MemberId == nil || member.Name == nil {
					continue
				}
				memberName := strings.TrimSpace(*member.Name)
				if memberName == targetName || strings.Contains(memberName, targetName) {
					return *member.MemberId, nil
				}
			}
			if resp.Data.HasMore != nil && *resp.Data.HasMore && resp.Data.PageToken != nil && *resp.Data.PageToken != "" {
				pageToken = *resp.Data.PageToken
				continue
			}
		}
		return "", errors.Errorf("member %s not found in chat %s", targetName, chatID)
	}
}

func copyFailureContent(conf common.UploadConf, relativePath string, copyErr error, mentionOpenID string) (string, error) {
	mentionName := strings.TrimSpace(conf.CopyFailureMentionName)
	if mentionName == "" {
		mentionName = "席伟杰"
	}
	firstLine := []map[string]string{
		{
			"tag":       "at",
			"user_id":   "all",
			"user_name": "所有人",
		},
		{
			"tag":  "text",
			"text": " 录像归档到 Server_Data 失败，请立即提醒" + mentionName + "修复",
		},
	}
	if mentionOpenID != "" {
		firstLine = append(firstLine, map[string]string{
			"tag":       "at",
			"user_id":   mentionOpenID,
			"user_name": mentionName,
		})
	} else {
		firstLine = append(firstLine, map[string]string{
			"tag":  "text",
			"text": "@" + mentionName,
		})
	}
	body := fmt.Sprintf(
		"相对路径：%s\n本机目录：%s\n长期目录：%s\n错误：%v",
		relativePath,
		storagepath.Resolve(conf.BaseDir, relativePath),
		storagepath.Resolve(conf.LongTermBaseDir, relativePath),
		copyErr,
	)
	content := map[string]any{
		"zh_cn": map[string]any{
			"title": "录像归档失败",
			"content": [][]map[string]string{
				firstLine,
				{
					{
						"tag":  "text",
						"text": body,
					},
				},
			},
		},
	}
	b, err := json.Marshal(content)
	if err != nil {
		return "", errors.Wrap(err, "marshal copy failure content")
	}
	return string(b), nil
}

func (l *DispatchLogic) recoverDispatching() error {
	if l.svcCtx.K8s == nil {
		return nil
	}
	tasks, err := l.svcCtx.DB.UploadTask.Query().
		Where(uploadtask.StatusEQ(uploadtask.StatusDISPATCHING), uploadtask.UpdatedAtLTE(time.Now().Add(-dispatchingStaleAfter))).
		Limit(100).
		All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query stale dispatching upload tasks")
	}
	namespace := l.svcCtx.Config.K8sJobConf.WithDefaults().Namespace
	for _, task := range tasks {
		name := jobName("upload", task.ID)
		if task.K8sJobName != nil && *task.K8sJobName != "" {
			name = *task.K8sJobName
		}
		exists, err := l.svcCtx.K8s.JobExists(l.ctx, namespace, name)
		if err != nil {
			return err
		}
		if exists {
			if err := l.svcCtx.DB.UploadTask.UpdateOneID(task.ID).SetStatus(uploadtask.StatusRUNNING).SetStartedAt(time.Now()).Exec(l.ctx); err != nil {
				return errors.Wrap(err, "recover running upload task")
			}
			l.Warnf("upload task recovered task_id=%d job=%s status=RUNNING", task.ID, name)
			continue
		}
		if err := l.svcCtx.DB.UploadTask.UpdateOneID(task.ID).SetStatus(uploadtask.StatusPENDING).Exec(l.ctx); err != nil {
			return errors.Wrap(err, "requeue stale upload task")
		}
		l.Warnf("upload task requeued task_id=%d missing_job=%s", task.ID, name)
	}
	return nil
}

func (l *DispatchLogic) dispatchPending() error {
	conf := l.svcCtx.Config.UploadConf.WithDefaults()
	if conf.DisableFileUpload {
		return nil
	}
	running, err := l.svcCtx.DB.UploadTask.Query().Where(uploadtask.StatusIn(uploadtask.StatusDISPATCHING, uploadtask.StatusRUNNING)).Count(l.ctx)
	if err != nil {
		return errors.Wrap(err, "count running upload tasks")
	}
	limit := conf.Concurrency - running
	if limit <= 0 {
		return nil
	}
	tasks, err := l.svcCtx.DB.UploadTask.Query().Where(uploadtask.StatusEQ(uploadtask.StatusPENDING)).Limit(limit).All(l.ctx)
	if err != nil {
		return errors.Wrap(err, "query pending upload tasks")
	}
	for _, task := range tasks {
		jobName := jobName("upload", task.ID)
		claimed, err := l.svcCtx.DB.UploadTask.Update().
			Where(uploadtask.ID(task.ID), uploadtask.StatusEQ(uploadtask.StatusPENDING)).
			SetStatus(uploadtask.StatusDISPATCHING).
			AddAttempts(1).
			SetK8sJobName(jobName).
			Save(l.ctx)
		if err != nil {
			return errors.Wrap(err, "mark upload dispatching")
		}
		if claimed == 0 {
			continue
		}
		l.Infof("upload task dispatching task_id=%d job=%s", task.ID, jobName)
		if l.svcCtx.K8s != nil {
			job := kubejob.Build(l.svcCtx.Config.K8sJobConf, kubejob.JobSpec{
				Name:     jobName,
				App:      "uploader-job",
				Image:    l.svcCtx.Config.K8sJobConf.Image,
				Args:     []string{"-f", "/etc/rm-monitor/config.yml", "-task", strconv.Itoa(task.ID)},
				MountPVC: true,
				CPU:      "100m",
				Memory:   "256Mi",
			})
			if err := l.svcCtx.K8s.CreateJob(l.ctx, l.svcCtx.Config.K8sJobConf.WithDefaults().Namespace, job); err != nil {
				_ = l.svcCtx.DB.UploadTask.UpdateOneID(task.ID).SetStatus(uploadtask.StatusFAILED).SetErrorMessage(err.Error()).Exec(l.ctx)
				l.Errorf("upload job create failed task_id=%d job=%s error=%v", task.ID, jobName, err)
				return err
			}
			l.Infof("upload job created task_id=%d job=%s", task.ID, jobName)
		}
		if err := l.svcCtx.DB.UploadTask.UpdateOneID(task.ID).SetStatus(uploadtask.StatusRUNNING).SetStartedAt(time.Now()).Exec(l.ctx); err != nil {
			return errors.Wrap(err, "mark upload running")
		}
		l.Infof("upload task running task_id=%d job=%s", task.ID, jobName)
	}
	return nil
}

func jobName(prefix string, id int) string {
	return strings.ToLower(fmt.Sprintf("%s-%d", prefix, id))
}
