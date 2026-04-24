package collector

import (
	"context"
	"fmt"

	"github.com/libvirt/libvirt-go"
)

// LibvirtCollector отвечает за сбор низкоуровневых метрик ВМ через CGO
type LibvirtCollector struct {
	URI string // URI для подключения, обычно "qemu:///system"
}

// NewLibvirtCollector инициализирует драйвер гипервизора
func NewLibvirtCollector(uri string) *LibvirtCollector {
	if uri == "" {
		uri = "qemu:///system"
	}
	return &LibvirtCollector{URI: uri}
}

// libvirtResult — внутренняя структура для безопасной передачи данных из CGO-горутины
type libvirtResult struct {
	cpuTime   uint64
	memoryRSS uint64
	err       error
}

// FetchStats извлекает процессорное время и RSS память для конкретного домена.
// Функция гарантированно возвращает управление при истечении ctx (Context Timeout).
func (c *LibvirtCollector) FetchStats(ctx context.Context, domainName string) (uint64, uint64, error) {
	// Создаем буферизированный канал на 1 элемент, чтобы горутина не заблокировалась (goroutine leak)
	resultCh := make(chan libvirtResult, 1)

	// Запускаем CGO-вызовы в изолированной анонимной горутине
	go func() {
		// 1. Подключение к демону libvirt
		conn, err := libvirt.NewConnect(c.URI)
		if err != nil {
			resultCh <- libvirtResult{err: fmt.Errorf("ошибка подключения к libvirt: %v", err)}
			return
		}
		defer conn.Close() // Обязательное освобождение памяти (защита от утечек)

		// 2. Поиск виртуальной машины
		domain, err := conn.LookupDomainByName(domainName)
		if err != nil {
			resultCh <- libvirtResult{err: fmt.Errorf("домен %s не найден: %v", domainName, err)}
			return
		}
		defer domain.Free() // Освобождение C-структуры домена

		var cpuTime, memoryRSS uint64

		// 3. Извлечение процессорного времени (параметр -1 возвращает сумму по всем vCPU)
		cpuStats, err := domain.GetCPUStats(-1, 1, 0)
		if err == nil && len(cpuStats) > 0 {
			cpuTime = cpuStats[0].CpuTime
		}

		// 4. Извлечение памяти (поиск тега Resident Set Size)
		memStats, err := domain.MemoryStats(uint32(libvirt.DOMAIN_MEMORY_STAT_NR), 0)
		if err == nil {
			for _, stat := range memStats {
				if stat.Tag == int32(libvirt.DOMAIN_MEMORY_STAT_RSS) {
					memoryRSS = stat.Val
					break
				}
			}
		}

		// Отправка успешного результата в канал
		resultCh <- libvirtResult{
			cpuTime:   cpuTime,
			memoryRSS: memoryRSS,
			err:       nil,
		}
	}()
	
	// Главный механизм отказоустойчивости (Select Multiplexer)
	select {
	case <-ctx.Done():
		// Если прошло 2 секунды (таймаут из main.go), мы не ждем CGO и возвращаем ошибку.
		// Это гарантирует, что агент не повиснет вместе с гипервизором.
		return 0, 0, fmt.Errorf("таймаут сбора метрик KVM: %v", ctx.Err())

	case res := <-resultCh:
		// Данные успешно собраны до истечения таймаута
		return res.cpuTime, res.memoryRSS, res.err
	}
}
