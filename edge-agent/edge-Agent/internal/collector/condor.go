package collector

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
)

// CondorCollector отвечает за взаимодействие с планировщиком задач HTCondor
type CondorCollector struct {
	BinPath string // Путь к исполняемому файлу (по умолчанию "condor_q")
}

// NewCondorCollector инициализирует сборщик метрик планировщика
func NewCondorCollector(binPath string) *CondorCollector {
	if binPath == "" {
		binPath = "condor_q" // Полагаемся на переменную среды $PATH
	}
	return &CondorCollector{BinPath: binPath}
}

// FetchLocalJobs запрашивает у демона HTCondor список задач (Job_ID),
// которые в данный момент исполняются на конкретном узле.
func (c *CondorCollector) FetchLocalJobs(ctx context.Context, nodeID string) ([]string, error) {
	// Формируем команду для HTCondor:
	// -constraint: Фильтруем задачи, запущенные только на нашем узле (RemoteHost)
	// -format: Извлекаем только чистые ID задач без лишнего текста и таблиц
	constraint := fmt.Sprintf("RemoteHost == \"%s\"", nodeID)

	// exec.CommandContext привязывает жизненный цикл процесса к ctx.
	// Если ctx истечет (таймаут 2 секунды), Go автоматически пошлет сигнал SIGKILL процессу condor_q.
	cmd := exec.CommandContext(ctx, c.BinPath,
		"-constraint", constraint,
		"-format", "%s\n", "GlobalJobId",
	)

	// Используем эффективные буферы для перехвата стандартного вывода (stdout) и ошибок (stderr)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr

	// Run() запускает команду синхронно, но с оглядкой на контекст
	err := cmd.Run()

	// Проверяем, не был ли процесс убит из-за нашего таймаута
	if ctx.Err() == context.DeadlineExceeded {
		return nil, fmt.Errorf("таймаут HTCondor: процесс condor_q был принудительно завершен ОС")
	}

	//  Проверяем системные ошибки самой утилиты (например, нет прав или демон лежит)
	if err != nil {
		return nil, fmt.Errorf("ошибка выполнения condor_q: %v, stderr: %s", err, stderr.String())
	}

	//  Парсинг успешного ответа
	rawOutput := strings.TrimSpace(stdout.String())

	// Если вывод пустой — значит, на узле сейчас нет активных задач
	if rawOutput == "" {
		return []string{}, nil
	}

	// Разбиваем строку по символу переноса строки в срез
	jobs := strings.Split(rawOutput, "\n")

	// Очищаем возможные пробелы
	var cleanJobs []string
	for _, job := range jobs {
		cleanJob := strings.TrimSpace(job)
		if cleanJob != "" {
			cleanJobs = append(cleanJobs, cleanJob)
		}
	}

	return cleanJobs, nil
}
