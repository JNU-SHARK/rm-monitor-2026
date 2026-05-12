package app

import (
	"os"
	"reflect"

	"github.com/pkg/errors"
	"sigs.k8s.io/yaml"
)

func LoadConfig(file string, out any) error {
	data, err := os.ReadFile(file)
	if err != nil {
		return errors.Wrap(err, "read config")
	}
	if err := yaml.Unmarshal(data, out); err != nil {
		return errors.Wrap(err, "parse config")
	}
	applyEnvOverrides(out)
	return nil
}

func MustLoadConfig(file string, out any) {
	if err := LoadConfig(file, out); err != nil {
		panic(err)
	}
}

func applyEnvOverrides(out any) {
	overrides := []struct {
		path []string
		envs []string
	}{
		{path: []string{"LarkConf", "AppId"}, envs: []string{"RM_MONITOR_LARK_APP_ID", "RM_MONITOR_FEISHU_APP_ID"}},
		{path: []string{"LarkConf", "AppSecret"}, envs: []string{"RM_MONITOR_LARK_APP_SECRET", "RM_MONITOR_FEISHU_APP_SECRET"}},
		{path: []string{"UploadConf", "BitableAppToken"}, envs: []string{"RM_MONITOR_BITABLE_APP_TOKEN", "RM_MONITOR_FEISHU_BITABLE_APP_TOKEN"}},
	}
	for _, override := range overrides {
		for _, env := range override.envs {
			value := os.Getenv(env)
			if value == "" {
				continue
			}
			if setStringField(out, override.path, value) {
				break
			}
		}
	}
}

func setStringField(out any, path []string, value string) bool {
	v := reflect.ValueOf(out)
	if v.Kind() != reflect.Pointer || v.IsNil() {
		return false
	}
	v = v.Elem()
	for _, name := range path {
		if v.Kind() == reflect.Pointer {
			if v.IsNil() {
				return false
			}
			v = v.Elem()
		}
		if v.Kind() != reflect.Struct {
			return false
		}
		v = v.FieldByName(name)
		if !v.IsValid() {
			return false
		}
	}
	if !v.CanSet() || v.Kind() != reflect.String {
		return false
	}
	v.SetString(value)
	return true
}
