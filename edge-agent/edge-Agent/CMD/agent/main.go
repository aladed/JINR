package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	// Здесь будут ваши внутренние пакеты.
	// Пути зависят от НАЗВАНИЯ. ПОКА НЕ ПОНЯТНО, ОБРАЗНО СДЕЛАЛ
	"govurun/internal/agent"
	"govurun/internal/collector"
	"govurun/internal/transport"
)

func main() {
	log.Println("[Init] Запуск инфраструктурного агента (Слои L1-L2)...")

	// Чтение конфигурации из переменных окружения (Environment Variables)
	nodeID := os.Getenv("NODE_ID")
	if nodeID == "" {
		nodeID = "compute-node-default" // Fallback для локального тестирования
	}
	kafkaBroker := os.Getenv("KAFKA_BROKER")
	if kafkaBroker == "" {
		kafkaBroker = "localhost:9092"
	}

	// Инициализация компонентов слоев L1 и L2

	// Слой L1: Сборщик сырых метрик (таймаут 2 секунды на опрос железа)
	extractor := &collector.ExtractionAgent{
		NodeID:  nodeID,
		Timeout: 2 * time.Second,
	}

	// Слой L2 (Математика): Процессор признаков (Ring Buffer и расчет Δ)
	processor := agent.NewFeatureProcessor(nodeID)

	// Слой L2 (Транспорт): Kafka Producer
	kafkaProducer, err := transport.NewKafkaProducer(kafkaBroker)
	if err != nil {
		log.Fatalf("[Fatal] Ошибка подключения к Kafka: %v", err)
	}
	// Гарантируем закрытие соединения с Kafka при выходе из программы
	defer kafkaProducer.Close()

	// Настройка бесконечного цикла (Ticker)
	// Агент будет просыпаться ровно раз в 5 секунд
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	// Настройка Graceful Shutdown (Изящное завершение)
	// Перехватываем сигналы от ОС (например, когда вы нажимаете Ctrl+C или Docker останавливает контейнер)
	stopChan := make(chan os.Signal, 1)
	signal.Notify(stopChan, os.Interrupt, syscall.SIGTERM)

	log.Printf("[Info] Агент узла %s успешно запущен. Начат сбор телеметрии.", nodeID)

	// Главный цикл приложения (Event Loop)
	for {
		select {
		case <-ticker.C:
			// Сработал таймер: запускаем такт сбора

			// Шаг 1: L1 собирает сырые данные (goroutines под капотом)
			rawMetrics := extractor.Collect()

			// Шаг 2: L2 вычисляет градиенты (Δ) и собирает вектор
			featureVector := processor.CalculateGradients(rawMetrics)

			// Шаг 3: L2 сериализует вектор в Protobuf/JSON и пушит в Kafka
			err := kafkaProducer.Push(featureVector)
			if err != nil {
				log.Printf("[Error] Не удалось отправить данные в Kafka: %v", err)
				// В случае ошибки агент не падает, а ждет следующего такта
			} else {
				log.Printf("[Success] Пакет от %s успешно отправлен.", nodeID)
			}

		case sig := <-stopChan:
			// Поступил сигнал остановки ОС
			log.Printf("[Shutdown] Получен сигнал %v. Изящное завершение работы...", sig)

			// Здесь можно добавить логику сохранения кэша L2 на диск, если нужно
			// defer kafkaProducer.Close() отработает автоматически

			log.Println("[Shutdown] Агент остановлен.")
			os.Exit(0)
		}
	}
}
