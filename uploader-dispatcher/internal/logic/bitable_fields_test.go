package logic

import (
	"testing"

	larkbitable "github.com/larksuite/oapi-sdk-go/v3/service/bitable/v1"
	"scutbot.cn/web/rm-monitor/pkg/bitableupload"
)

func TestBitableFieldsOrder(t *testing.T) {
	fields := bitableFields(false)
	want := []bitableFieldSpec{
		{name: bitableupload.FieldMatch, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldStage, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldRedTeam, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldBlueTeam, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldRole, fieldType: larkbitable.TypeSingleSelect},
		{name: bitableupload.FieldFilePath, fieldType: larkbitable.TypeText},
		{name: bitableupload.FieldBilibili, fieldType: larkbitable.TypeUrl},
	}
	if len(fields) != len(want) {
		t.Fatalf("len(fields) = %d, want %d", len(fields), len(want))
	}
	for i := range want {
		if fields[i] != want[i] {
			t.Fatalf("fields[%d] = %#v, want %#v", i, fields[i], want[i])
		}
	}
}

func TestBitableFieldsWithAttachment(t *testing.T) {
	fields := bitableFields(true)
	last := fields[len(fields)-1]
	want := bitableFieldSpec{name: bitableupload.FieldAttachment, fieldType: larkbitable.TypeAttachment}
	if last != want {
		t.Fatalf("last field = %#v, want %#v", last, want)
	}
}
