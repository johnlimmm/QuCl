#ifndef COSIM_AGENT_APP_H
#define COSIM_AGENT_APP_H

#include "ns3/application.h"
#include "ns3/socket.h"

#include <cstdint>
#include <functional>
#include <string>

namespace ns3::cosim
{

// Classical status and results only; never transfer quantum states or fidelity.
struct Result
{
    uint64_t id{};
    uint32_t processor{};
    std::string state; // QUEUED, RUNNING, COMPLETED, REJECTED, or CANCELLED.
    std::string code;  // Status reason such as OK, DUPLICATE, or RESOURCE_CONFLICT.
    int m1{-1};        // BSM bit; -1 means the result is not available.
    int m2{-1};
};

// UDP helper for readiness notifications and background traffic.
// Callbacks forward events without making quantum scheduling decisions.
class CosimAgentApp : public Application
{
  public:
    static TypeId GetTypeId();
    // UDP callbacks run inside the ns-3 packet receive event.
    using PacketCallback = std::function<void(Ptr<Packet>, const Address &)>;
    void BindUdp(uint16_t port, PacketCallback callback);
    void SendUdp(Ptr<Packet> packet, const Address &destination);

  private:
    void HandleRead(Ptr<Socket> socket);
    void DoDispose() override;
    Ptr<Socket> m_socket;
    PacketCallback m_packetCallback;
};

} // namespace ns3::cosim

#endif
