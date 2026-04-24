package transport

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/segmentio/kafka-go"
)

// KafkaProducer инкапсулирует логику отправки данных в шину Kafka.
type KafkaProducer struct {
	writer *kafka.Writer
}

// NewKafkaProducer создает и настраивает асинхронный продюсер.
func NewKafkaProducer(broker string) (*KafkaProducer, error) {
	// Настройка параметров записи для высоконагруженных систем (HPC)
	w := &kafka.Writer{
		Addr:     kafka.TCP(broker),
		Topic:    "telemetry.raw",     // Тот самый топик, который будет слушать Влад
		Balancer: &kafka.LeastBytes{}, // Равномерное распределение нагрузки между партициями

		// Асинхронные настройки для максимальной производительности:
		Async:        true, // Не блокируем поток выполнения при отправке
		BatchSize:    100,  // Группируем сообщения для экономии сетевых вызовов
		BatchTimeout: 10 * time.Millisecond,

		// Настройки надежности
		MaxAttempts:  3,
		RequiredAcks: kafka.RequireOne, // Ждем подтверждения хотя бы от одного брокера (баланс скорость/надежность)

		// Логирование ошибок доставки в фоне
		Completion: func(messages []kafka.Message, err error) {
			if err != nil {
				log.Printf("[L2 Kafka Error] Ошибка фоновой доставки пакета: %v", err)
			}
		},
	}

	return &KafkaProducer{writer: w}, nil
}

// Push принимает бинарные данные (Protobuf) и отправляет их в очередь.
func (kp *KafkaProducer) Push(data []byte) error {
	// Создаем сообщение
	msg := kafka.Message{
		Value: data,
		Time:  time.Now(),
	}

	// Так как у нас Async: true, метод WriteMessages отработает мгновенно (поместит в буфер).
	// Реальная сетевая отправка произойдет в фоне.
	err := kp.writer.WriteMessages(context.Background(), msg)
	if err != nil {
		return fmt.Errorf("ошибка помещения сообщения в буфер Kafka: %v", err)
	}

	return nil
}

// Close корректно завершает работу, дожидаясь отправки всех сообщений из буфера.
func (kp *KafkaProducer) Close() error {
	log.Println("[Transport] Завершение работы Kafka Producer, очистка буферов...")
	return kp.writer.Close()
}
