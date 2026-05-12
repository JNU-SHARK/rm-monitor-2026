package bitableupload

import (
	"fmt"
	"strings"

	"scutbot.cn/web/rm-monitor/ent"
)

const (
	FieldRole       = "视角"
	FieldMatch      = "场次"
	FieldStage      = "阶段"
	FieldType       = "类型"
	FieldRedTeam    = "红方"
	FieldBlueTeam   = "蓝方"
	FieldFilePath   = "文件路径"
	FieldBilibili   = "视频链接"
	FieldAttachment = "录像"
)

func TableName(event, zone string) string {
	return strings.TrimSpace(fmt.Sprintf("%s-%s", event, zone))
}

func MatchName(m *ent.Match) string {
	if m == nil {
		return ""
	}
	red := teamName(m.Edges.RedTeam)
	blue := teamName(m.Edges.BlueTeam)
	return fmt.Sprintf("%d. %s VS %s", m.Order, red, blue)
}

func TeamName(t *ent.Team) string {
	return teamName(t)
}

func teamName(t *ent.Team) string {
	if t == nil {
		return ""
	}
	school := strings.TrimSpace(t.SchoolName)
	name := strings.TrimSpace(t.Name)
	switch {
	case school == "":
		return name
	case name == "":
		return school
	default:
		return school + "-" + name
	}
}

func RecordFields(m *ent.Match, role string) map[string]interface{} {
	return map[string]interface{}{
		FieldRole:     role,
		FieldMatch:    MatchName(m),
		FieldStage:    StageLabel(m),
		FieldRedTeam:  TeamName(m.Edges.RedTeam),
		FieldBlueTeam: TeamName(m.Edges.BlueTeam),
	}
}

func RecordFieldsWithPath(m *ent.Match, role, filePath string) map[string]interface{} {
	fields := RecordFields(m, role)
	fields[FieldFilePath] = filePath
	return fields
}

func AttachmentValue(fileToken, name string) []map[string]interface{} {
	return []map[string]interface{}{{
		"file_token": fileToken,
		"name":       name,
	}}
}

func StageLabel(m *ent.Match) string {
	if m == nil {
		return ""
	}
	if m.MatchSlug != nil {
		slug := strings.TrimSpace(*m.MatchSlug)
		if hasChinese(slug) {
			return slug
		}
	}
	raw := strings.TrimSpace(m.MatchType)
	if raw == "" {
		return ""
	}
	labels := map[string]string{
		"GROUP":          "小组赛",
		"GROUP_STAGE":    "小组赛",
		"KNOCKOUT":       "淘汰赛",
		"KNOCKOUT_STAGE": "淘汰赛",
		"ELIMINATION":    "淘汰赛",
		"PLAYOFF":        "淘汰赛",
		"PLAY_OFF":       "淘汰赛",
		"ROUND_OF_32":    "1/16决赛",
		"ROUND_OF_16":    "1/8决赛",
		"EIGHTH_FINAL":   "1/8决赛",
		"QUARTER_FINAL":  "1/4决赛",
		"SEMI_FINAL":     "半决赛",
		"FINAL":          "决赛",
		"GRAND_FINAL":    "总决赛",
		"THIRD_PLACE":    "季军赛",
		"BRONZE":         "季军赛",
		"TEST":           "测试",
	}
	key := strings.ToUpper(strings.NewReplacer("-", "_", " ", "_").Replace(raw))
	if label, ok := labels[key]; ok {
		return label
	}
	return raw
}

func hasChinese(s string) bool {
	for _, r := range s {
		if r >= '\u4e00' && r <= '\u9fff' {
			return true
		}
	}
	return false
}
