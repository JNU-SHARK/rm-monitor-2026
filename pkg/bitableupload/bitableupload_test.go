package bitableupload

import (
	"testing"

	"scutbot.cn/web/rm-monitor/ent"
)

func TestTableName(t *testing.T) {
	if got := TableName("RoboMaster", "南部"); got != "RoboMaster-南部" {
		t.Fatalf("TableName() = %q", got)
	}
}

func TestRecordFields(t *testing.T) {
	slug := "小组赛"
	m := &ent.Match{
		Order:     3,
		MatchType: "GROUP",
		MatchSlug: &slug,
		Edges: ent.MatchEdges{
			RedTeam:  &ent.Team{Name: "红队", SchoolName: "红校"},
			BlueTeam: &ent.Team{Name: "蓝队", SchoolName: "蓝校"},
		},
	}
	fields := RecordFields(m, "big")
	if fields[FieldRole] != "big" {
		t.Fatalf("role = %v", fields[FieldRole])
	}
	if fields[FieldMatch] != "3. 红校-红队 VS 蓝校-蓝队" {
		t.Fatalf("match = %v", fields[FieldMatch])
	}
	if fields[FieldStage] != "小组赛" || fields[FieldRedTeam] != "红校-红队" || fields[FieldBlueTeam] != "蓝校-蓝队" {
		t.Fatalf("unexpected fields: %#v", fields)
	}
}

func TestStageLabel(t *testing.T) {
	cases := []struct {
		name string
		m    *ent.Match
		want string
	}{
		{name: "group", m: &ent.Match{MatchType: "GROUP"}, want: "小组赛"},
		{name: "play off", m: &ent.Match{MatchType: "play-off"}, want: "淘汰赛"},
		{name: "slug", m: &ent.Match{MatchType: "GROUP", MatchSlug: ptr("自定义阶段")}, want: "自定义阶段"},
		{name: "fallback", m: &ent.Match{MatchType: "CUSTOM"}, want: "CUSTOM"},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			if got := StageLabel(tt.m); got != tt.want {
				t.Fatalf("StageLabel() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestAttachmentValue(t *testing.T) {
	value := AttachmentValue("boxabc", "source.flv")
	if len(value) != 1 || value[0]["file_token"] != "boxabc" || value[0]["name"] != "source.flv" {
		t.Fatalf("unexpected attachment value: %#v", value)
	}
}

func ptr[T any](v T) *T {
	return &v
}
