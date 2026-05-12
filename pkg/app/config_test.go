package app

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadConfigAppliesLarkEnvOverrides(t *testing.T) {
	t.Setenv("RM_MONITOR_LARK_APP_ID", "cli_test")
	t.Setenv("RM_MONITOR_LARK_APP_SECRET", "secret_test")
	t.Setenv("RM_MONITOR_BITABLE_APP_TOKEN", "base_test")

	dir := t.TempDir()
	file := filepath.Join(dir, "config.yml")
	if err := os.WriteFile(file, []byte(`
LarkConf:
  AppId: ""
  AppSecret: ""
UploadConf:
  BitableAppToken: ""
`), 0o600); err != nil {
		t.Fatal(err)
	}

	var cfg struct {
		LarkConf struct {
			AppId     string
			AppSecret string
		}
		UploadConf struct {
			BitableAppToken string
		}
	}
	if err := LoadConfig(file, &cfg); err != nil {
		t.Fatal(err)
	}
	if cfg.LarkConf.AppId != "cli_test" {
		t.Fatalf("AppId = %q", cfg.LarkConf.AppId)
	}
	if cfg.LarkConf.AppSecret != "secret_test" {
		t.Fatalf("AppSecret = %q", cfg.LarkConf.AppSecret)
	}
	if cfg.UploadConf.BitableAppToken != "base_test" {
		t.Fatalf("BitableAppToken = %q", cfg.UploadConf.BitableAppToken)
	}
}
