package agent

import (
	"edge-Agent/internal/collector"
	"time"
)

// FeatureVector представляет финальный набор обработанных данных для отправки в Kafka
type FeatureVector struct {
	NodeID    string    `json:"node_id"`
	Timestamp time.Time `json:"timestamp"`

	// Абсолютные значения (L1)
	CPU_Now    uint64 `json:"cpu_now"`
	RAM_RSS    uint64 `json:"ram_rss"`
	ActiveJobs int    `json:"active_jobs"`

	// Градиенты / Дельты (L2)
	CPU_Delta   float64 `json:"cpu_delta"`     // Скорость изменения CPU
	RAM_Delta   float64 `json:"ram_delta"`     // Скорость изменения RAM
	NetErrDelta int     `json:"net_err_delta"` // Рост сетевых ошибок
}

// FeatureProcessor инкапсулирует логику вычисления признаков
type FeatureProcessor struct {
	NodeID  string
	History *RingBuffer // Ссылка на наш кольцевой буфер
}

// NewFeatureProcessor создает новый процессор с буфером на 300 записей (5 минут)
func NewFeatureProcessor(nodeID string) *FeatureProcessor {
	return &FeatureProcessor{
		NodeID:  nodeID,
		History: NewRingBuffer(300),
	}
}

// CalculateGradients берет сырые данные из L1 и превращает их в вектор признаков
func (p *FeatureProcessor) CalculateGradients(raw *collector.RawMetrics) FeatureVector {
	// Сохраняем новые данные в кольцевой буфер
	p.History.Push(*raw)

	// Пытаемся достать пару (Текущее, Предыдущее) для расчета Δ
	current, previous, ok := p.History.GetDeltaPair()

	// Инициализируем вектор базовыми значениями
	vector := FeatureVector{
		NodeID:     p.NodeID,
		Timestamp:  raw.Timestamp,
		CPU_Now:    raw.CPUTime,
		RAM_RSS:    raw.MemoryRSS,
		ActiveJobs: len(raw.CondorJobs),
	}

	// Если данных для Δ еще нет (первый запуск), возвращаем вектор с нулевыми градиентами
	if !ok {
		return vector
	}

	//  РАСЧЕТ МАТЕМАТИКИ (Градиенты)

	// Δt в секундах (обычно 5 секунд, но мы считаем точно)
	dt := current.Timestamp.Sub(previous.Timestamp).Seconds()
	if dt <= 0 {
		dt = 1 // Защита от деления на ноль
	}

	// Градиент CPU: (T1 - T0) / dt
	// Так как CPUTime в наносекундах, мы получаем процент загрузки за интервал
	vector.CPU_Delta = float64(current.CPUTime-previous.CPUTime) / dt

	// Градиент RAM (изменение в байтах в секунду)
	vector.RAM_Delta = float64(current.MemoryRSS-previous.MemoryRSS) / dt

	// Рост сетевых ошибок (чистая разница)
	vector.NetErrDelta = current.NetworkErrs - previous.NetworkErrs

	return vector
}
