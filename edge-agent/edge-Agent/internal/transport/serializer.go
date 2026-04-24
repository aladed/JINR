package transport

import (
	"edge-Agent/internal/agent"
	"fmt"

	"google.golang.org/protobuf/proto"
)

// SerializeTelemetry преобразует высокоуровневую структуру FeatureVector
// в компактный бинарный формат Protobuf.
func SerializeTelemetry(v agent.FeatureVector) ([]byte, error) {
	// Создаем структуру, сгенерированную компилятором protoc
	// (Предполагается, что вы запустили генерацию кода)
	pb := &TelemetryPacket{
		NodeId:      v.NodeID,
		Timestamp:   v.Timestamp.Unix(), // Конвертируем время в Unix-секунды для ML
		CpuNow:      v.CPU_Now,
		RamRss:      v.RAM_RSS,
		ActiveJobs:  int32(v.ActiveJobs),
		CpuDelta:    v.CPU_Delta,
		RamDelta:    v.RAM_Delta,
		NetErrDelta: int32(v.NetErrDelta),
	}

	// Выполняем бинарную маршализацию
	data, err := proto.Marshal(pb)
	if err != nil {
		return nil, fmt.Errorf("ошибка Protobuf сериализации: %v", err)
	}

	return data, nil
}
