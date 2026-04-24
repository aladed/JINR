package collector

import (
	"context"
	"fmt"
	"time"

	"github.com/gosnmp/gosnmp"
)

// SNMPCollector отвечает за сбор метрик с сетевого коммутатора (Top-of-Rack Switch)
type SNMPCollector struct {
	Target    string // IP адрес коммутатора (например, 192.168.1.1)
	Community string // SNMP Community String (обычно "public" для чтения)
	Port      uint16 // Порт (по умолчанию 161)
}

// NewSNMPCollector инициализирует драйвер сетевого опроса
func NewSNMPCollector(target, community string) *SNMPCollector {
	if target == "" {
		target = "127.0.0.1"
	}
	if community == "" {
		community = "public"
	}
	return &SNMPCollector{
		Target:    target,
		Community: community,
		Port:      161,
	}
}

// snmpResult — внутренняя структура для безопасной передачи данных из горутины
type snmpResult struct {
	errorsCount int
	err         error
}

// FetchNetworkErrors запрашивает количество ошибок на интерфейсах коммутатора
func (c *SNMPCollector) FetchNetworkErrors(ctx context.Context) (int, error) {
	// Буферизированный канал для защиты от утечки горутин (Goroutine Leak)
	resultCh := make(chan snmpResult, 1)

	go func() {
		// 1. Инициализация изолированного клиента (НЕ используем gosnmp.Default!)
		// Это гарантирует потокобезопасность, если агент опрашивает несколько свитчей параллельно.
		client := &gosnmp.GoSNMP{
			Target:    c.Target,
			Port:      c.Port,
			Community: c.Community,
			Version:   gosnmp.Version2c,
			Timeout:   2 * time.Second, // Внутренний таймаут UDP пакета
		}

		err := client.Connect()
		if err != nil {
			resultCh <- snmpResult{err: fmt.Errorf("ошибка подключения к коммутатору %s: %v", c.Target, err)}
			return
		}
		defer client.Conn.Close() // Гарантированное закрытие сокета

		// 2. Список OID (Object Identifiers) для опроса
		// 1.3.6.1.2.1.2.2.1.14 - ifInErrors (Входящие ошибки пакетов)
		// 1.3.6.1.2.1.2.2.1.20 - ifOutErrors (Исходящие ошибки пакетов)
		oids := []string{
			"1.3.6.1.2.1.2.2.1.14.1", // Пример для порта №1
			"1.3.6.1.2.1.2.2.1.20.1",
		}

		// 3. Выполнение SNMP GET запроса
		result, err := client.Get(oids)
		if err != nil {
			resultCh <- snmpResult{err: fmt.Errorf("ошибка SNMP GET: %v", err)}
			return
		}

		// 4. Агрегация результатов
		var totalErrors int
		for _, variable := range result.Variables {
			// SNMP может возвращать разные типы данных (Integer, Counter32, Gauge32)
			// Безопасное приведение типов (Type Assertion)
			switch val := variable.Value.(type) {
			case int:
				totalErrors += val
			case uint:
				totalErrors += int(val)
			case uint64:
				totalErrors += int(val)
			}
		}

		resultCh <- snmpResult{errorsCount: totalErrors, err: nil}
	}()

	// ---------------------------------------------------------
	// Блок синхронизации с основным контекстом агента
	// ---------------------------------------------------------
	select {
	case <-ctx.Done():
		// ОС или Ticker отменили операцию, UDP пакет потерялся в сети
		return 0, fmt.Errorf("SNMP таймаут: ответ от %s не получен в отведенное время", c.Target)

	case res := <-resultCh:
		// Успешный ответ
		return res.errorsCount, res.err
	}
}
