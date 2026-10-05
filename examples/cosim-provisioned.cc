// Run native Q2NS SwapApp and TeleportationApp with common IPC and packet tracing.
// Sessions start locally at R; real R-to-B Q2NS packets connect quantum operations.
#include "../model/cosim-bridge.h"
#include "ns3/core-module.h"
#include "ns3/cosim-agent-app.h"
#include "ns3/internet-module.h"
#include "ns3/network-module.h"
#include "ns3/point-to-point-module.h"
#include "ns3/q2ns-qnode.h"
#include "ns3/q2ns-qstate-registry.h"
#include "ns3/q2ns-swap-app.h"
#include "ns3/q2ns-teleportation-app.h"
#include "ns3/traffic-control-helper.h"
#include "ns3/traffic-control-layer.h"

#include <iostream>
#include <map>
#include <vector>

using namespace ns3;
using namespace ns3::cosim;
using namespace ns3::cosim::bridge;

namespace
{
constexpr uint32_t A = 0, R = 1, B = 2;
constexpr uint16_t PORT = 9000, BG_PORT = 9001;

// Readiness adapter around the unchanged native Q2NS SwapApp packet/state machine.
class SwapResourceGate
{
  public:
    explicit SwapResourceGate(Ptr<q2ns::SwapApp> value) : app(value) {}
    void AddGatedSession(const q2ns::SwapApp::SessionConfig &cfg,
                         const q2ns::SwapApp::ExternalResources &resources)
    {
        app->AddExternalSession(cfg, resources);
        gates.emplace(cfg.sid, Gate{});
    }
    bool StartExternalSession(uint64_t sid)
    {
        auto &gate = gates.at(sid);
        if (gate.requested)
            return false;
        gate.requested = true;
        Try(sid);
        return true;
    }
    bool NotifyExternalResourcesReady(uint64_t sid)
    {
        auto &gate = gates.at(sid);
        if (gate.ready)
            return false;
        gate.ready = true;
        Try(sid);
        return true;
    }

  private:
    struct Gate
    {
        bool requested{false}, ready{false}, issued{false};
    };
    std::map<uint64_t, Gate> gates;
    Ptr<q2ns::SwapApp> app;
    void Try(uint64_t sid)
    {
        auto &gate = gates.at(sid);
        if (gate.requested && gate.ready && !gate.issued)
        {
            gate.issued = true;
            Require(app->RequestExternalBsm(sid), "duplicate gated Swap BSM");
        }
    }
};

// Fixed 60-byte background header; no fidelity or quantum state in packets.
// txSequence links each receive trace to the causal send event.
class Q2nsHeader : public Header
{
  public:
    uint8_t kind{5}; // 2: native result, 4: result-link background, 5: local event
    uint64_t session{1}, request{1}, message{1}, pairA{1}, pairB{2}, output{3};
    int m1{-1}, m2{-1};
    uint64_t txSequence{0};

    static TypeId GetTypeId()
    {
        static TypeId tid =
            TypeId("ns3::cosim::Q2nsHeader").SetParent<Header>().AddConstructor<Q2nsHeader>();
        return tid;
    }
    TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }
    uint32_t GetSerializedSize() const override
    {
        return 60;
    }
    void Serialize(Buffer::Iterator i) const override
    {
        i.WriteU8(1);
        i.WriteU8(kind);
        for (auto v : {session, request, message, pairA, pairB, output})
        {
            i.WriteHtonU64(v);
        }
        i.WriteU8(m1 < 0 ? 255 : m1);
        i.WriteU8(m2 < 0 ? 255 : m2);
        i.WriteHtonU64(txSequence);
    }
    uint32_t Deserialize(Buffer::Iterator i) override
    {
        Require(i.ReadU8() == 1, "invalid packet version");
        kind = i.ReadU8();
        Require(kind == 4, "invalid packet kind");
        session = i.ReadNtohU64();
        request = i.ReadNtohU64();
        message = i.ReadNtohU64();
        pairA = i.ReadNtohU64();
        pairB = i.ReadNtohU64();
        output = i.ReadNtohU64();
        auto a = i.ReadU8(), b = i.ReadU8();
        m1 = a == 255 ? -1 : a;
        m2 = b == 255 ? -1 : b;
        txSequence = i.ReadNtohU64();
        return GetSerializedSize();
    }
    void Print(std::ostream &os) const override
    {
        os << session << '/' << request << '/' << message;
    }
};

// Observation tags add no wire bytes; Swap results retain the Q2NS CtrlHeader payload.
class SwapTraceTag : public Tag
{
  public:
    uint64_t session{}, message{}, sequence{};
    bool teleport{false};
    static TypeId GetTypeId()
    {
        static TypeId tid =
            TypeId("ns3::cosim::SwapTraceTag").SetParent<Tag>().AddConstructor<SwapTraceTag>();
        return tid;
    }
    TypeId GetInstanceTypeId() const override
    {
        return GetTypeId();
    }
    uint32_t GetSerializedSize() const override
    {
        return 25;
    }
    void Serialize(TagBuffer b) const override
    {
        b.WriteU64(session);
        b.WriteU64(message);
        b.WriteU64(sequence);
        b.WriteU8(teleport);
    }
    void Deserialize(TagBuffer b) override
    {
        session = b.ReadU64();
        message = b.ReadU64();
        sequence = b.ReadU64();
        teleport = b.ReadU8();
    }
    void Print(std::ostream &os) const override
    {
        os << session << '/' << message;
    }
};

Q2nsHeader ReadSwapResult(Ptr<const Packet> packet)
{
    SwapTraceTag tag;
    Require(packet->PeekPacketTag(tag), "missing Q2NS packet provenance");
    Q2nsHeader h;
    h.kind = 2;
    h.session = tag.session;
    h.message = tag.message;
    h.txSequence = tag.sequence;
    if (tag.teleport)
    {
        uint8_t bits[2];
        Require(packet->GetSize() == 2, "invalid teleport packet size");
        packet->CopyData(bits, 2);
        Require(bits[0] <= 1 && bits[1] <= 1, "invalid teleport bits");
        h.m1 = bits[0];
        h.m2 = bits[1];
        // The trace pair fields do not describe teleport inputs; its adapter uses typed handles.
        h.pairA = h.pairB = h.output = 0;
    }
    else
    {
        uint8_t payload[10];
        Require(packet->GetSize() >= 10, "short swap result");
        packet->CopyData(payload, 10);
        uint64_t sid;
        std::memcpy(&sid, payload, sizeof(sid));
        Require(sid == tag.session && payload[8] <= 1 && payload[9] <= 1, "swap payload mismatch");
        h.m1 = payload[8];
        h.m2 = payload[9];
    }
    return h;
}

struct Flow
{
    int64_t start, interval;
    uint32_t count, bytes;
};

struct TraceBuffer
{
    uint64_t sequence{0};
    std::vector<std::string> rows;
    uint64_t Add(const std::string &type, uint32_t node, const Q2nsHeader &h,
                 const std::string &cause, uint32_t bytes = 0, int64_t depth = -1)
    {
        uint64_t id = sequence++;
        std::ostringstream out;
        out << "TRACE " << id << ' ' << Simulator::Now().GetNanoSeconds() << ' ' << type << ' '
            << node << ' ' << h.message << ' ' << cause << ' ' << h.session << ' ' << h.request
            << ' ' << h.pairA << ' ' << h.pairB << ' ' << h.output << ' ' << h.m1 << ' ' << h.m2
            << ' ' << bytes << ' ' << unsigned(h.kind) << ' ' << depth;
        rows.push_back(out.str());
        return id;
    }
};

std::string EventIdString(uint64_t id)
{
    return "n:" + std::to_string(id);
}

Q2nsHeader ReadMessage(Ptr<Packet> packet)
{
    Q2nsHeader h;
    Require(packet->GetSize() >= h.GetSerializedSize(), "short UDP payload");
    packet->RemoveHeader(h);
    return h;
}

// Queue/PHY packets include protocol headers; validate both UDP port and message kind.
Q2nsHeader ReadWire(Ptr<const Packet> packet, bool hasPpp = true)
{
    auto copy = packet->Copy();
    PppHeader ppp;
    Ipv4Header ipv4;
    UdpHeader udp;
    if (hasPpp)
        copy->RemoveHeader(ppp);
    copy->RemoveHeader(ipv4);
    Require(ipv4.GetProtocol() == 17, "Q2ns expects UDP only");
    copy->RemoveHeader(udp);
    SwapTraceTag provenance;
    auto h = copy->PeekPacketTag(provenance) ? ReadSwapResult(copy) : ReadMessage(copy);
    Require(udp.GetDestinationPort() == (h.kind == 2 ? PORT : BG_PORT) ||
                (provenance.teleport && h.kind == 2 && udp.GetDestinationPort() >= 10000),
            "kind/port mismatch");
    return h;
}

void TracePhy(TraceBuffer *trace, std::string type, uint32_t node, Ptr<const Packet> packet)
{
    auto h = ReadWire(packet);
    trace->Add(type, node, h, EventIdString(h.txSequence), packet->GetSize());
}

void TraceQueue(TraceBuffer *trace, std::string type, uint32_t node, Queue<Packet> *queue,
                Ptr<const Packet> packet)
{
    auto h = ReadWire(packet);
    // Queue callbacks observe the depth after the enqueue/dequeue event.
    trace->Add(type, node, h, EventIdString(h.txSequence), packet->GetSize(), queue->GetNPackets());
}

void TraceTcDrop(TraceBuffer *trace, uint32_t node, Ptr<const Packet> packet)
{
    // TrafficControl may drop on device backpressure even without a QueueDisc.
    // The PPP header has not yet been added at this trace point.
    auto h = ReadWire(packet, false);
    trace->Add("TC_DROP", node, h, EventIdString(h.txSequence), packet->GetSize());
}

void RunParticipant(Wire &wire)
{
    Time::SetResolution(Time::NS);
    Simulator::SetImplementation(CreateObject<SteppingSimulator>());
    ObjectFactory factory;
    factory.SetTypeId(VisibleScheduler::GetTypeId());
    Simulator::SetScheduler(factory);
    wire.Send("HELLO COSIM_PROVISIONED 4");
    std::string tag;
    uint64_t resultRate;
    int64_t resultDelay;
    uint32_t resultBytes, queuePackets, sessionCount;
    Parse(wire.Read(), tag, resultRate, resultDelay, resultBytes, queuePackets, sessionCount);
    Require(tag == "CONFIG" && resultRate > 0 && resultDelay >= 0 && resultBytes >= 60 &&
                resultBytes <= 1400 && queuePackets > 0 && queuePackets <= 65536 &&
                sessionCount > 0 && sessionCount <= 64,
            "invalid Hybrid CONFIG");
    struct Session
    {
        int64_t startAt;
        std::string root;
        uint64_t message;
        unsigned protocol;
    };
    std::map<uint64_t, Session> sessions;
    std::vector<uint64_t> sessionOrder;
    for (uint32_t slot = 0; slot < sessionCount; ++slot)
    {
        uint64_t sid;
        Session item;
        Parse(wire.Read(), tag, sid, item.startAt, item.root, item.protocol);
        Require(item.protocol <= 1, "unsupported protocol");
        Require(tag == "SESSION" && sid > 0 && sessions.count(sid) == 0 && item.startAt >= 0 &&
                    item.startAt <= MAX_TIME,
                "invalid SESSION");
        // Derive packet IDs from configuration order without arithmetic on large session IDs.
        item.message = slot + 1;
        sessions.emplace(sid, item);
        sessionOrder.push_back(sid);
    }
    std::vector<Flow> flows;
    for (const auto &side : {"result"})
    {
        Flow flow;
        std::string name;
        Parse(wire.Read(), tag, name, flow.start, flow.interval, flow.count, flow.bytes);
        Require(tag == "FLOW" && name == side && flow.start >= 0 && flow.start <= MAX_TIME &&
                    flow.interval > 0 && flow.interval <= MAX_TIME && flow.count <= 2000 &&
                    flow.bytes >= 60 && flow.bytes <= 1400,
                "invalid FLOW");
        Require(flow.count == 0 || (flow.count - 1) <= (MAX_TIME - flow.start) / flow.interval,
                "background time overflow");
        flows.push_back(flow);
    }

    q2ns::QStateRegistry registry;
    NodeContainer nodes;
    for (int i = 0; i < 3; ++i)
        nodes.Add(CreateObject<q2ns::QNode>(registry));
    // Use actual Q2NS nodes/apps; NetSquid remains the sole quantum-state owner.
    InternetStackHelper internet;
    internet.SetIpv6StackInstall(false);
    internet.Install(nodes);
    TraceBuffer trace;
    for (auto node : {R, B})
    {
        nodes.Get(node)->GetObject<TrafficControlLayer>()->TraceConnectWithoutContext(
            "TcDrop", MakeBoundCallback(&TraceTcDrop, &trace, node));
    }
    auto link = [&](uint32_t from, uint32_t to, uint64_t rate, int64_t delay, const char *subnet)
    {
        PointToPointHelper helper;
        helper.SetDeviceAttribute("DataRate", DataRateValue(DataRate(rate)));
        helper.SetDeviceAttribute("InterframeGap", TimeValue(NanoSeconds(0)));
        helper.SetChannelAttribute("Delay", TimeValue(NanoSeconds(delay)));
        helper.SetQueue("ns3::DropTailQueue<Packet>", "MaxSize",
                        QueueSizeValue(QueueSize(std::to_string(queuePackets) + "p")));
        auto devices = helper.Install(NodeContainer(nodes.Get(from), nodes.Get(to)));
        Ipv4AddressHelper address;
        address.SetBase(subnet, "255.255.255.0");
        auto interfaces = address.Assign(devices);
        // Remove automatic QueueDiscs so packet waiting uses one device FIFO.
        TrafficControlHelper traffic;
        traffic.Uninstall(devices);
        for (uint32_t i = 0; i < 2; ++i)
        {
            uint32_t node = i == 0 ? from : to;
            devices.Get(i)->TraceConnectWithoutContext(
                "PhyTxBegin", MakeBoundCallback(&TracePhy, &trace, std::string("PHY_TX"), node));
            devices.Get(i)->TraceConnectWithoutContext(
                "PhyTxEnd", MakeBoundCallback(&TracePhy, &trace, std::string("PHY_TX_END"), node));
            devices.Get(i)->TraceConnectWithoutContext(
                "PhyTxDrop",
                MakeBoundCallback(&TracePhy, &trace, std::string("PHY_TX_DROP"), node));
            devices.Get(i)->TraceConnectWithoutContext(
                "PhyRxDrop",
                MakeBoundCallback(&TracePhy, &trace, std::string("PHY_RX_DROP"), node));
            auto queue = DynamicCast<PointToPointNetDevice>(devices.Get(i))->GetQueue();
            for (const auto &entry :
                 std::vector<std::pair<std::string, std::string>>{{"Enqueue", "QUEUE_ENQUEUE"},
                                                                  {"Dequeue", "QUEUE_DEQUEUE"},
                                                                  {"Drop", "QUEUE_DROP"}})
            {
                queue->TraceConnectWithoutContext(
                    entry.first,
                    MakeBoundCallback(&TraceQueue, &trace, entry.second, node, PeekPointer(queue)));
            }
            devices.Get(i)->TraceConnectWithoutContext(
                "PhyRxEnd", MakeBoundCallback(&TracePhy, &trace, std::string("PHY_RX"), node));
        }
        return interfaces;
    };
    auto rb = link(R, B, resultRate, resultDelay, "10.1.1.0");
    std::map<uint64_t, Ptr<q2ns::TeleportationApp>> teleSource, teleSink;
    auto repeater = CreateObject<q2ns::SwapApp>();
    SwapResourceGate swapGate(repeater);
    auto endpoint = CreateObject<q2ns::SwapApp>();
    nodes.Get(R)->AddApplication(repeater);
    nodes.Get(B)->AddApplication(endpoint);
    repeater->SetPayloadBytes(resultBytes);
    repeater->SetStartTime(NanoSeconds(0));
    endpoint->SetStartTime(NanoSeconds(0));
    std::map<uint64_t, std::string> startCauses, resultCauses, completionCauses, correctionCauses,
        localCauses, triggerCauses;
    std::map<uint64_t, bool> resourcesReady;
    uint32_t correctionsApplied = 0;
    auto nativeTrace = [&](std::string type, uint32_t node, uint64_t sid, const std::string &cause,
                           int m1 = -1, int m2 = -1)
    {
        Q2nsHeader h;
        h.kind = 5;
        h.session = sid;
        h.message = 0;
        h.pairA = 101;
        h.pairB = 102;
        h.output = 103;
        h.m1 = m1;
        h.m2 = m2;
        if (sessions.at(sid).protocol)
            h.pairA = h.pairB = h.output = 0;
        trace.Add(type, node, h, cause);
    };
    auto eligibilityCause = [&](uint64_t sid)
    {
        Q2nsHeader h;
        h.session = sid;
        h.message = 0;
        if (sessions.at(sid).protocol)
            h.pairA = h.pairB = h.output = 0;
        return EventIdString(trace.Add("SESSION_ELIGIBLE", R, h, triggerCauses.at(sid)));
    };
    repeater->SetExternalQuantumCallbacks(
        [&](uint64_t sid, const q2ns::SwapApp::ExternalResources &resources)
        {
            Require(resources.pairPrev == 101 && resources.pairNext == 102 &&
                        resources.outputPair == 103,
                    "Q2NS logical EPR mapping mismatch");
            Q2nsHeader h;
            h.session = sid;
            h.message = 0;
            auto id = trace.Add("BSM_REQUEST", R, h, eligibilityCause(sid));
            nativeTrace("Q2NS_BSM_REQUEST", R, sid, EventIdString(id));
        },
        {});
    endpoint->SetExternalQuantumCallbacks(
        {},
        [&](uint64_t sid, uint64_t output, uint8_t m1, uint8_t m2)
        {
            Require(output == 103, "Q2NS correction pair mapping mismatch");
            Q2nsHeader h;
            h.kind = 2;
            h.session = sid;
            h.message = sessions.at(sid).message;
            h.m1 = m1;
            h.m2 = m2;
            auto id = trace.Add("CORRECTION_REQUEST", B, h, resultCauses.at(sid));
            nativeTrace("Q2NS_CORRECTION_REQUEST", B, sid, EventIdString(id), m1, m2);
        });
    repeater->SetExternalPacketObservers(
        [&](uint64_t sid, uint8_t m1, uint8_t m2, Ptr<Packet> packet)
        {
            Q2nsHeader h;
            h.kind = 2;
            h.session = sid;
            h.message = sessions.at(sid).message;
            h.m1 = m1;
            h.m2 = m2;
            auto id = trace.Add("RESULT_TX", R, h, localCauses.at(sid), packet->GetSize());
            SwapTraceTag provenance;
            provenance.session = sid;
            provenance.message = h.message;
            provenance.sequence = id;
            packet->AddPacketTag(provenance);
        },
        {});
    endpoint->SetExternalPacketObservers(
        {},
        [&](uint64_t sid, uint8_t m1, uint8_t m2, Ptr<Packet> packet)
        {
            auto h = ReadSwapResult(packet);
            Require(h.session == sid && h.m1 == m1 && h.m2 == m2,
                    "SwapApp receive/payload mismatch");
            resultCauses[sid] = EventIdString(
                trace.Add("RESULT_RX", B, h, EventIdString(h.txSequence), packet->GetSize()));
        });
    repeater->m_traceBSMDone.ConnectWithoutContext(Callback<void, uint64_t, Time, uint8_t, uint8_t>(
        [&](uint64_t sid, Time, uint8_t m1, uint8_t m2)
        {
            Q2nsHeader h;
            h.kind = 2;
            h.session = sid;
            h.message = 0;
            h.m1 = m1;
            h.m2 = m2;
            localCauses[sid] =
                EventIdString(trace.Add("BSM_RESULT_LOCAL", R, h, completionCauses.at(sid)));
            nativeTrace("Q2NS_BSM_DONE", R, sid, completionCauses.at(sid), m1, m2);
        }));
    repeater->m_traceCtrlSent.ConnectWithoutContext(
        Callback<void, uint64_t, Time, uint8_t, uint8_t>(
            [&](uint64_t sid, Time, uint8_t m1, uint8_t m2)
            { nativeTrace("Q2NS_CTRL_SENT", R, sid, localCauses.at(sid), m1, m2); }));
    endpoint->m_traceFrameResolved.ConnectWithoutContext(
        Callback<void, uint64_t, Time, uint8_t, uint8_t>(
            [&](uint64_t sid, Time, uint8_t m1, uint8_t m2)
            { nativeTrace("Q2NS_FRAME_RESOLVED", B, sid, resultCauses.at(sid), m1, m2); }));
    endpoint->m_traceCorrectionApplied.ConnectWithoutContext(Callback<void, uint64_t, Time>(
        [&](uint64_t sid, Time)
        {
            ++correctionsApplied;
            nativeTrace("Q2NS_CORRECTION_APPLIED", B, sid, correctionCauses.at(sid));
        }));
    for (auto sid : sessionOrder)
    {
        if (sessions.at(sid).protocol)
        {
            auto source = CreateObject<q2ns::TeleportationApp>();
            auto sink = CreateObject<q2ns::TeleportationApp>();
            teleSource[sid] = source;
            teleSink[sid] = sink;
            const uint16_t port = 10000 + sessions.at(sid).message - 1;
            for (auto app : {source, sink})
            {
                app->SetAttribute("SessionId", UintegerValue(sid));
                app->SetAttribute("Role", StringValue(app == source ? "source" : "sink"));
                app->SetAttribute("ClassicalProtocol", StringValue("udp"));
                app->SetAttribute("CtrlPort", UintegerValue(port));
                app->SetAttribute("Peer", Ipv4AddressValue(rb.GetAddress(1)));
                nodes.Get(app == source ? R : B)->AddApplication(app);
                app->SetStartTime(NanoSeconds(0));
            }
            source->ConfigureExternal(
                {201, 202, 203},
                [&, sid](uint64_t actual, const q2ns::TeleportationApp::ExternalResources &r)
                {
                    Require(actual == sid && r.input == 201 && r.epr == 202 && r.output == 203,
                            "teleport handle mismatch");
                    Q2nsHeader h;
                    h.session = sid;
                    h.message = 0;
                    h.pairA = h.pairB = h.output = 0;
                    auto id = trace.Add("BSM_REQUEST", R, h, eligibilityCause(sid));
                    nativeTrace("Q2NS_TELEPORT_BSM_REQUEST", R, sid, EventIdString(id));
                },
                {});
            sink->ConfigureExternal(
                {201, 202, 203}, {},
                [&, sid](uint64_t actual, uint64_t target, uint8_t m1, uint8_t m2)
                {
                    Require(actual == sid && target == 203, "teleport target mismatch");
                    Q2nsHeader h;
                    h.kind = 2;
                    h.session = sid;
                    h.message = sessions.at(sid).message;
                    h.pairA = h.pairB = h.output = 0;
                    h.m1 = m1;
                    h.m2 = m2;
                    auto id = trace.Add("CORRECTION_REQUEST", B, h, resultCauses.at(sid));
                    nativeTrace("Q2NS_TELEPORT_CORRECTION_REQUEST", B, sid, EventIdString(id), m1,
                                m2);
                });
            source->SetExternalPacketObservers(
                [&, sid](uint64_t actual, uint8_t m1, uint8_t m2, Ptr<Packet> packet)
                {
                    Require(actual == sid, "teleport tx session mismatch");
                    Q2nsHeader h;
                    h.kind = 2;
                    h.session = sid;
                    h.message = sessions.at(sid).message;
                    h.pairA = h.pairB = h.output = 0;
                    h.m1 = m1;
                    h.m2 = m2;
                    auto id = trace.Add("RESULT_TX", R, h, localCauses.at(sid), packet->GetSize());
                    SwapTraceTag tag;
                    tag.session = sid;
                    tag.message = h.message;
                    tag.sequence = id;
                    tag.teleport = true;
                    packet->AddPacketTag(tag);
                },
                {});
            sink->SetExternalPacketObservers(
                {},
                [&, sid](uint64_t actual, uint8_t m1, uint8_t m2, Ptr<Packet> packet)
                {
                    auto h = ReadSwapResult(packet);
                    Require(actual == sid && h.session == sid && h.m1 == m1 && h.m2 == m2,
                            "teleport rx mismatch");
                    resultCauses[sid] = EventIdString(trace.Add(
                        "RESULT_RX", B, h, EventIdString(h.txSequence), packet->GetSize()));
                    nativeTrace("Q2NS_TELEPORT_CTRL_RECEIVED", B, sid, resultCauses.at(sid), m1,
                                m2);
                });
            source->m_traceSourceBellDone.ConnectWithoutContext(Callback<void, uint64_t, Time>(
                [&, sid](uint64_t actual, Time)
                {
                    Require(actual == sid, "teleport BSM callback mismatch");
                    Q2nsHeader h;
                    h.kind = 2;
                    h.session = sid;
                    h.message = 0;
                    h.pairA = h.pairB = h.output = 0;
                    localCauses[sid] = EventIdString(
                        trace.Add("BSM_RESULT_LOCAL", R, h, completionCauses.at(sid)));
                    nativeTrace("Q2NS_TELEPORT_BSM_DONE", R, sid, completionCauses.at(sid));
                }));
            sink->m_traceSinkCorrection.ConnectWithoutContext(Callback<void, uint64_t, Time>(
                [&, sid](uint64_t actual, Time)
                {
                    Require(actual == sid, "teleport correction callback mismatch");
                    ++correctionsApplied;
                    nativeTrace("Q2NS_TELEPORT_CORRECTION_APPLIED", B, sid,
                                correctionCauses.at(sid));
                }));
            continue;
        }
        q2ns::SwapApp::SessionConfig cfg;
        cfg.sid = sid;
        cfg.role = q2ns::SwapApp::Role::Repeater;
        cfg.ctrlPort = PORT;
        cfg.nextEndAddr = rb.GetAddress(1);
        swapGate.AddGatedSession(cfg, {101, 102, 103});
        cfg.role = q2ns::SwapApp::Role::Next;
        cfg.applyCorrections = true;
        endpoint->AddExternalSession(cfg, {101, 102, 103});
    }
    // Q2NS PacketSink/SwapApp handle the actual result socket and header decoding.
    // Background traffic uses a separate port and cannot trigger quantum requests.
    std::map<uint32_t, Ptr<CosimAgentApp>> background;
    for (auto node : {R, B})
    {
        auto app = CreateObject<CosimAgentApp>();
        nodes.Get(node)->AddApplication(app);
        background[node] = app;
        app->BindUdp(BG_PORT,
                     [&, node](Ptr<Packet> packet, const Address &)
                     {
                         auto bytes = packet->GetSize();
                         auto h = ReadMessage(packet);
                         Require(h.session == 0 && (node == B && h.kind == 4),
                                 "unexpected background destination");
                         trace.Add("BACKGROUND_RX", node, h, EventIdString(h.txSequence), bytes);
                     });
    }
    // Register application initialization before possible session starts at t=0.
    for (uint32_t i = 0; i < nodes.GetN(); ++i)
        nodes.Get(i)->Initialize();
    // TeleportationApp schedules its own zero-delay session activation from
    // StartApplication. Drain startup before registering user requests so a
    // t=0 Teleport request cannot be overtaken by a later t=0 Swap request.
    size_t startupEvents = 0;
    while (NextTime() == 0)
    {
        Require(++startupEvents <= MAX_BATCH, "application startup event limit");
        Simulator::Run();
    }
    // Deliver readiness traces at t=0 through the normal bridge boundary.
    Simulator::ScheduleNow([] {});
    // Background packets use a separate port and never invoke quantum requests.
    const auto flow = flows.at(0);
    for (uint32_t i = 0; i < flow.count; ++i)
    {
        Q2nsHeader h;
        h.kind = 4;
        h.session = h.request = h.pairA = h.pairB = h.output = 0;
        h.message = 100000 + i;
        Simulator::Schedule(NanoSeconds(flow.start + i * flow.interval),
                            [&, h, flow]() mutable
                            {
                                h.txSequence = trace.Add("BACKGROUND_TX", R, h, "-", flow.bytes);
                                auto packet = Create<Packet>(flow.bytes - h.GetSerializedSize());
                                packet->AddHeader(h);
                                background.at(R)->SendUdp(
                                    packet, InetSocketAddress(rb.GetAddress(1), BG_PORT));
                            });
    }
    // Equal-time starts retain configuration order; there is no command packet.
    for (auto sid : sessionOrder)
    {
        const auto item = sessions.at(sid);
        Simulator::Schedule(NanoSeconds(item.startAt),
                            [&, sid, item]
                            {
                                Q2nsHeader h;
                                h.session = sid;
                                h.message = 0;
                                if (item.protocol)
                                    h.pairA = h.pairB = h.output = 0;
                                startCauses[sid] =
                                    EventIdString(trace.Add("SESSION_START", R, h, item.root));
                                triggerCauses[sid] = startCauses[sid];
                                Require(item.protocol ? teleSource.at(sid)->RequestExternalBsm(sid)
                                                      : swapGate.StartExternalSession(sid),
                                        "duplicate Q2NS session start");
                            });
    }
    wire.Send("NODES " + std::to_string(nodes.GetN()) + " " +
              std::to_string(nodes.Get(A)->GetId()) + " " + std::to_string(nodes.Get(R)->GetId()) +
              " " + std::to_string(nodes.Get(B)->GetId()));
    wire.Send("READY 0 " + std::to_string(NextTime()));
    while (true)
    {
        auto line = wire.Read();
        if (line == "QUIT")
        {
            wire.Send("BYE");
            break;
        }
        std::istringstream prefix(line);
        prefix >> tag;
        if (tag == "ADVANCE")
        {
            int64_t at;
            Parse(line, tag, at);
            auto now = Simulator::Now().GetNanoSeconds();
            Require(at >= now && at <= MAX_TIME, "time reversal or overflow");
            Require(NextTime() == -1 || NextTime() >= at, "ADVANCE skips ns-3 event");
            if (at > now)
                Simulator::Schedule(NanoSeconds(at - now), [] {});
            size_t count = 0;
            while (NextTime() == at)
            {
                Require(++count <= MAX_BATCH, "same-timestamp event limit");
                Simulator::Run();
            }
            Require(Simulator::Now().GetNanoSeconds() == at, "ns-3 boundary mismatch");
            Require(registry.GetStatesSortedById().empty(),
                    "Q2NS unexpectedly owns quantum states");
            wire.Send("BOUNDARY " + std::to_string(at) + " " + std::to_string(NextTime()) + " " +
                      std::to_string(trace.rows.size()));
            for (const auto &row : trace.rows)
                wire.Send(row);
            trace.rows.clear();
        }
        else if (tag == "STATUS")
        {
            size_t local = 0;
            for (uint32_t i = 0; i < nodes.GetN(); ++i)
                local += DynamicCast<q2ns::QNode>(nodes.Get(i))->GetLocalQubits().size();
            wire.Send("Q2NS_STATUS " + std::to_string(registry.GetStatesSortedById().size()) + " " +
                      std::to_string(local) + " " + std::to_string(sessionCount) + " " +
                      std::to_string(correctionsApplied));
        }
        else if (tag == "RESOURCE_READY")
        {
            int64_t at;
            size_t count;
            Parse(line, tag, at, count);
            Require(at == Simulator::Now().GetNanoSeconds() && count <= sessionCount,
                    "invalid resource notification boundary");
            for (size_t i = 0; i < count; ++i)
            {
                uint64_t sid;
                std::string cause;
                Parse(wire.Read(), tag, sid, cause);
                Require(tag == "RESOURCE" && sessions.count(sid) && !resourcesReady[sid],
                        "duplicate or unknown resource notification");
                resourcesReady[sid] = true;
                Simulator::ScheduleNow(
                    [&, sid, cause]
                    {
                        Q2nsHeader h;
                        h.session = sid;
                        h.message = 0;
                        if (sessions.at(sid).protocol)
                            h.pairA = h.pairB = h.output = 0;
                        triggerCauses[sid] =
                            EventIdString(trace.Add("Q2NS_RESOURCES_READY", R, h, cause));
                        if (sessions.at(sid).protocol)
                        {
                            Require(teleSink.at(sid)->NotifyExternalResourcesReady(sid),
                                    "sink readiness failed");
                            Require(teleSource.at(sid)->NotifyExternalResourcesReady(sid),
                                    "source readiness failed");
                        }
                        else
                            Require(swapGate.NotifyExternalResourcesReady(sid),
                                    "Swap readiness failed");
                    });
            }
            wire.Send("RESOURCES_NOTIFIED " + std::to_string(NextTime()));
        }
        else if (tag == "INJECT")
        {
            int64_t at;
            size_t count, correctionCount;
            Parse(line, tag, at, count, correctionCount);
            Require(at == Simulator::Now().GetNanoSeconds() && count <= sessionCount &&
                        correctionCount <= sessionCount,
                    "invalid INJECT");
            for (size_t i = 0; i < count; ++i)
            {
                Result result;
                std::string cause;
                uint64_t request;
                Parse(wire.Read(), tag, result.id, request, result.m1, result.m2, cause);
                Require(tag == "RESULT" && sessions.count(result.id) == 1 && request == 1 &&
                            result.m1 >= 0 && result.m1 <= 1 && result.m2 >= 0 && result.m2 <= 1,
                        "invalid BSM result");
                result.processor = R;
                result.state = "COMPLETED";
                result.code = "OK";
                completionCauses[result.id] = cause;
                Simulator::ScheduleNow(
                    [&, result]
                    {
                        Require(
                            sessions.at(result.id).protocol
                                ? teleSource.at(result.id)->CompleteExternalBsm(
                                      result.id, result.m1, result.m2)
                                : repeater->CompleteExternalBsm(result.id, result.m1, result.m2),
                            "duplicate BSM completion");
                    });
            }
            for (size_t i = 0; i < correctionCount; ++i)
            {
                uint64_t sid;
                std::string cause;
                Parse(wire.Read(), tag, sid, cause);
                Require(tag == "CORRECT" && sessions.count(sid), "invalid correction completion");
                correctionCauses[sid] = cause;
                Simulator::ScheduleNow(
                    [&, sid]
                    {
                        Require(sessions.at(sid).protocol
                                    ? teleSink.at(sid)->CompleteExternalCorrection(sid)
                                    : endpoint->CompleteExternalCorrection(sid),
                                "duplicate correction completion");
                    });
            }
            wire.Send("INJECTED " + std::to_string(NextTime()));
        }
        else
            throw std::runtime_error("unknown Q2ns command");
    }
    Simulator::Destroy();
}
} // namespace

int main(int argc, char **argv)
{
    int bridgeFd = -1;
    CommandLine cmd(__FILE__);
    cmd.AddValue("bridgeFd", "Inherited Unix socket from run_provisioned.py", bridgeFd);
    cmd.Parse(argc, argv);
    try
    {
        Require(bridgeFd >= 0, "launch with contrib/cosim/python/run_provisioned.py");
        Wire wire(bridgeFd);
        RunParticipant(wire);
        close(bridgeFd);
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "cosim-provisioned: " << error.what() << '\n';
        Simulator::Destroy();
        return 1;
    }
}
