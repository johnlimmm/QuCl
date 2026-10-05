#include "cosim-agent-app.h"

#include "ns3/inet-socket-address.h"

#include <stdexcept>
#include <utility>

namespace ns3::cosim
{

// Register the application with the ns-3 object system.
NS_OBJECT_ENSURE_REGISTERED(CosimAgentApp);

TypeId CosimAgentApp::GetTypeId()
{
    static TypeId tid = TypeId("ns3::cosim::CosimAgentApp")
                            .SetParent<Application>()
                            .SetGroupName("Cosim")
                            .AddConstructor<CosimAgentApp>();
    return tid;
}

void CosimAgentApp::BindUdp(uint16_t port, PacketCallback callback)
{
    if (m_socket)
    {
        throw std::logic_error("UDP socket already bound");
    }
    // The scenario installs InternetStack before binding UDP sockets.
    m_socket = Socket::CreateSocket(GetNode(), TypeId::LookupByName("ns3::UdpSocketFactory"));
    if (m_socket->Bind(InetSocketAddress(Ipv4Address::GetAny(), port)) != 0)
    {
        throw std::runtime_error("UDP bind failed");
    }
    m_packetCallback = std::move(callback);
    m_socket->SetRecvCallback(MakeCallback(&CosimAgentApp::HandleRead, this));
}

void CosimAgentApp::SendUdp(Ptr<Packet> packet, const Address &destination)
{
    const auto bytes = packet->GetSize();
    if (!m_socket || m_socket->SendTo(packet, 0, destination) != static_cast<int>(bytes))
    {
        throw std::runtime_error("UDP send failed");
    }
}

void CosimAgentApp::HandleRead(Ptr<Socket> socket)
{
    Address sender;
    while (auto packet = socket->RecvFrom(sender))
    {
        if (!m_packetCallback)
        {
            throw std::logic_error("UDP receive callback is not installed");
        }
        m_packetCallback(packet, sender);
    }
}

void CosimAgentApp::DoDispose()
{
    if (m_socket)
    {
        m_socket->SetRecvCallback(MakeNullCallback<void, Ptr<Socket>>());
        m_socket->Close();
        m_socket = nullptr;
    }
    m_packetCallback = {};
    Application::DoDispose();
}

} // namespace ns3::cosim
