// Native Q2NS state execution for the standalone comparison. No timed processor
// or memory-noise model is added: QNode operations execute at the current time.
#pragma once

#include "ns3/q2ns-analysis.h"
#include "ns3/q2ns-qchannel.h"
#include "ns3/q2ns-qgate.h"
#include "ns3/q2ns-qnet-device.h"
#include "ns3/q2ns-qnetworker.h"
#include "ns3/q2ns-qstate-dm.h"
#include "ns3/q2ns-qubit.h"
#include <iomanip>
#include <functional>

class Q2nsChainBackend
{
  public:
    using Qubit = std::shared_ptr<q2ns::Qubit>;
    struct Chain
    {
        Qubit input, a, ra, rb, b;
        qpp::cmat target;
        unsigned delivered{0};
        bool measured{false}, handedOff{false}, teleMeasured{false}, complete{false};
    };
    std::map<uint64_t, Chain> chains;
    std::vector<std::string> rows;
    std::unique_ptr<q2ns::QNetworker> delivery;
    ns3::Ptr<q2ns::QNode> alice, repeater, bob;

    void Initialize(ns3::NodeContainer nodes, q2ns::QStateRegistry &registry, int64_t seed,
                    int64_t raDelay, int64_t rbDelay,
                    const std::map<uint64_t, std::string> &inputs,
                    std::function<void(uint64_t)> ready)
    {
        using namespace ns3;
        using ns3::cosim::bridge::Require;
        registry.SetDefaultBackend(q2ns::QStateBackend::DM);
        q2ns::QStateDM rng(1);
        rng.AssignStreams(seed);
        alice = DynamicCast<q2ns::QNode>(nodes.Get(0));
        repeater = DynamicCast<q2ns::QNode>(nodes.Get(1));
        bob = DynamicCast<q2ns::QNode>(nodes.Get(2));
        delivery = std::make_unique<q2ns::QNetworker>(*repeater);
        auto channel = [&](Ptr<q2ns::QNode> target, int64_t delay)
        {
            auto sourceDevice = CreateObject<q2ns::QNetDevice>();
            auto targetDevice = CreateObject<q2ns::QNetDevice>();
            repeater->AddDevice(sourceDevice);
            target->AddDevice(targetDevice);
            auto link = CreateObject<q2ns::QChannel>();
            link->SetDelay(NanoSeconds(delay));
            link->Connect(sourceDevice, targetDevice);
            auto iface = delivery->AddInterface(sourceDevice);
            delivery->AddRoute(target->GetId(), iface);
            return sourceDevice;
        };
        channel(alice, raDelay);
        channel(bob, rbDelay);
        auto receive = [this, ready](Qubit q)
        {
            for (auto &entry : chains)
            {
                auto &c = entry.second;
                if (q == c.a || q == c.b)
                {
                    ns3::cosim::bridge::Require(c.delivered < 2, "duplicate quantum arrival");
                    Record("DELIVERED", entry.first, q->GetQubitId());
                    if (++c.delivered == 2)
                        ready(entry.first);
                    return;
                }
            }
            throw std::runtime_error("unknown quantum arrival");
        };
        alice->SetRecvCallback(Callback<void, Qubit>(receive));
        bob->SetRecvCallback(Callback<void, Qubit>(receive));
        for (const auto &entry : inputs)
        {
            const auto sid = entry.first;
            auto &c = chains[sid];
            auto left = repeater->CreateBellPair(), right = repeater->CreateBellPair();
            c.a = left.first;
            c.ra = left.second;
            c.b = right.first;
            c.rb = right.second;
            c.input = alice->CreateQubit("input-" + std::to_string(sid));
            const auto &s = entry.second;
            Require(s == "0" || s == "1" || s == "+" || s == "-" || s == "+i" || s == "-i",
                    "invalid native input");
            if (s == "1") alice->Apply(q2ns::gates::X(), {c.input});
            if (s != "0" && s != "1") alice->Apply(q2ns::gates::H(), {c.input});
            if (s == "-") alice->Apply(q2ns::gates::Z(), {c.input});
            if (s == "+i") alice->Apply(q2ns::gates::S(), {c.input});
            if (s == "-i") alice->Apply(q2ns::gates::SDG(), {c.input});
            c.target = std::dynamic_pointer_cast<q2ns::QStateDM>(alice->GetState(c.input))->GetRho();
            Record("CREATED", sid, c.a->GetQubitId(), c.b->GetQubitId());
            // Native device/channel delivery adopts the same physical qubits at
            // A/B; no replacement Bell pair is created by TeleportationApp.
            Require(delivery->Send(c.a, alice->GetId()) && delivery->Send(c.b, bob->GetId()),
                    "native quantum send failed");
        }
    }

    std::pair<uint8_t, uint8_t> Bsm(uint64_t parent, bool teleport)
    {
        using ns3::cosim::bridge::Require;
        auto &c = chains.at(parent);
        Require(c.delivered == 2, "BSM before delivery");
        if (!teleport)
        {
            Require(!c.measured, "duplicate swap BSM");
            c.measured = true;
            return repeater->MeasureBell(c.ra, c.rb);
        }
        Require(c.handedOff && !c.teleMeasured, "invalid native handoff");
        c.teleMeasured = true;
        Record("CONSUMED", parent, c.a->GetQubitId(), c.b->GetQubitId());
        return alice->MeasureBell(c.input, c.a);
    }

    void Correct(uint64_t parent, bool teleport, uint8_t m1, uint8_t m2)
    {
        using ns3::cosim::bridge::Require;
        auto &c = chains.at(parent);
        Require(teleport ? c.teleMeasured && !c.complete : c.measured && !c.handedOff,
                "invalid native correction");
        if (m2) bob->Apply(q2ns::gates::X(), {c.b});
        if (m1) bob->Apply(q2ns::gates::Z(), {c.b});
        if (!teleport)
        {
            c.handedOff = true;
            Record("HANDOFF", parent, c.a->GetQubitId(), c.b->GetQubitId());
            return;
        }
        c.complete = true;
        auto state = std::dynamic_pointer_cast<q2ns::QStateDM>(bob->GetState(c.b));
        Require(state && state->GetRho().rows() == 2, "invalid native output state");
        q2ns::QStateDM target(c.target);
        std::ostringstream out;
        out << std::setprecision(17) << "NATIVE_STATE " << parent << ' '
            << ns3::Simulator::Now().GetNanoSeconds() << ' '
            << q2ns::analysis::Fidelity(*state, target);
        for (int i = 0; i < 2; ++i)
            for (int j = 0; j < 2; ++j)
                out << ' ' << state->GetRho()(i,j).real() << ' ' << state->GetRho()(i,j).imag();
        rows.push_back(out.str());
    }

    void Record(const std::string &kind, uint64_t sid, uint64_t q1, uint64_t q2 = 0)
    {
        std::ostringstream out;
        out << "NATIVE_RESOURCE " << kind << ' ' << sid << ' '
            << ns3::Simulator::Now().GetNanoSeconds() << ' ' << q1 << ' ' << q2;
        rows.push_back(out.str());
    }
};
