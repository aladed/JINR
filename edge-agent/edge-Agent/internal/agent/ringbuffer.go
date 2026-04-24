package agent

import (
	"sync"
	"time"

	"edge-Agent/internal/collector"
)

// RingBuffer реализует потокобезопасный кольцевой буфер фиксированного размера
// с нулевой аллокацией (Zero-allocation) при добавлении новых элементов.
type RingBuffer struct {
	buffer   []collector.RawMetrics // Статичный массив под данные (заранее выделен)
	capacity int                    // Максимальная вместимость (например, 300 для 5 минут при 1 Гц)
	head     int                    // Указатель на текущую позицию записи
	isFull   bool                   // Флаг полного заполнения (когда head сделал полный круг)
	mu       sync.RWMutex           // Защита от состояния гонки при параллельном чтении/записи
}

// NewRingBuffer инициализирует кольцевой буфер, аллоцируя память единожды.
func NewRingBuffer(capacity int) *RingBuffer {
	return &RingBuffer{
		// Выделяем память сразу (make с указанием capacity и length),
		// чтобы избежать внутренних реаллокаций слайса (growslice) в Go.
		buffer:   make([]collector.RawMetrics, capacity),
		capacity: capacity,
		head:     0,
		isFull:   false,
	}
}

// Push добавляет новый снимок метрик в буфер.
// Выполняется за константное время O(1) без выделения памяти в куче.
func (rb *RingBuffer) Push(metrics collector.RawMetrics) {
	rb.mu.Lock()
	defer rb.mu.Unlock()

	// Записываем новые данные поверх старых по индексу head
	rb.buffer[rb.head] = metrics

	// Сдвигаем указатель вперед. Если достигли конца массива — возвращаемся в 0 (Кольцо)
	rb.head = (rb.head + 1) % rb.capacity

	// Если указатель вернулся в 0, значит мы переписали самые старые данные
	if rb.head == 0 {
		rb.isFull = true
	}
}

// GetDeltaMetrics извлекает текущее (t0) и прошлое (t-1) состояния
// для расчета градиентов (скорости изменения нагрузки).
func (rb *RingBuffer) GetDeltaPair() (current, previous collector.RawMetrics, ok bool) {
	rb.mu.RLock()
	defer rb.mu.RUnlock()

	// Если буфер еще не накопил хотя бы 2 значения, дельту считать рано
	if !rb.isFull && rb.head < 2 {
		return collector.RawMetrics{}, collector.RawMetrics{}, false
	}

	// Вычисляем индекс текущего (последнего записанного) элемента
	currentIndex := (rb.head - 1 + rb.capacity) % rb.capacity
	current = rb.buffer[currentIndex]

	// Вычисляем индекс предыдущего элемента
	prevIndex := (rb.head - 2 + rb.capacity) % rb.capacity
	previous = rb.buffer[prevIndex]

	return current, previous, true
}

// GetHistoricalSnapshot возвращает самый старый элемент в буфере (например, данные 5 минут назад).
// Это нужно для расчета длинных трендов (Delta_15m), о которых говорится в Главе 3.4
func (rb *RingBuffer) GetHistoricalSnapshot() (collector.RawMetrics, bool) {
	rb.mu.RLock()
	defer rb.mu.RUnlock()

	if !rb.isFull && rb.head == 0 {
		return collector.RawMetrics{}, false
	}

	var oldestIndex int
	if rb.isFull {
		// Если кольцо заполнено, самый старый элемент находится прямо ПОД текущим указателем
		oldestIndex = rb.head
	} else {
		// Если кольцо еще заполняется (старт системы), самый старый элемент — это первый (индекс 0)
		oldestIndex = 0
	}

	return rb.buffer[oldestIndex], true
}
