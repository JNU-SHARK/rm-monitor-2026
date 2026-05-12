package logc

import (
	"context"
	"fmt"
	"log/slog"
)

func Debug(ctx context.Context, args ...any) {
	slog.Default().DebugContext(ctx, fmt.Sprint(args...))
}

func Info(ctx context.Context, args ...any) {
	slog.Default().InfoContext(ctx, fmt.Sprint(args...))
}

func Warn(ctx context.Context, args ...any) {
	slog.Default().WarnContext(ctx, fmt.Sprint(args...))
}

func Error(ctx context.Context, args ...any) {
	slog.Default().ErrorContext(ctx, fmt.Sprint(args...))
}

func Debugf(ctx context.Context, format string, args ...any) {
	slog.Default().DebugContext(ctx, fmt.Sprintf(format, args...))
}

func Infof(ctx context.Context, format string, args ...any) {
	slog.Default().InfoContext(ctx, fmt.Sprintf(format, args...))
}

func Warnf(ctx context.Context, format string, args ...any) {
	slog.Default().WarnContext(ctx, fmt.Sprintf(format, args...))
}

func Errorf(ctx context.Context, format string, args ...any) {
	slog.Default().ErrorContext(ctx, fmt.Sprintf(format, args...))
}
