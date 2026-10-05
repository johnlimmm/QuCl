// Single-threaded ns-3 event stepping and line-oriented IPC.
#ifndef COSIM_BRIDGE_H
#define COSIM_BRIDGE_H
#include "ns3/default-simulator-impl.h"
#include "ns3/map-scheduler.h"
#include "ns3/simulator.h"
#include <cerrno>
#include <cstring>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <unistd.h>
namespace ns3::cosim::bridge
{
// Bound integer ns to the exact integer range of NetSquid double timestamps.
constexpr int64_t MAX_TIME = 9007199254740991LL;
// Reject unbounded same-time event cascades and oversized message batches.
constexpr size_t MAX_BATCH = 100000;

inline void Require(bool condition, const std::string &message)
{
    if (!condition)
    {
        throw std::runtime_error(message);
    }
}

// Stop in PreEventHook still allows the current event to execute before Run returns.
// The next Run resumes the queue, allowing one-event steps through the public hook.
class SteppingSimulator : public DefaultSimulatorImpl
{
  public:
    void PreEventHook(const EventId &) override
    {
        Stop();
    }
};

// Retain the installed scheduler to inspect its next event through public PeekNext().
// Used by one single-threaded participant; never access a private ns-3 event queue.
class VisibleScheduler : public MapScheduler
{
  public:
    inline static VisibleScheduler *instance = nullptr;
    static TypeId GetTypeId()
    {
        static TypeId tid = TypeId("ns3::cosim::VisibleScheduler")
                                .SetParent<MapScheduler>()
                                .AddConstructor<VisibleScheduler>();
        return tid;
    }
    VisibleScheduler()
    {
        instance = this;
    }
};

inline int64_t NextTime()
{
    // The Python participant interprets -1 as no pending event.
    auto *scheduler = VisibleScheduler::instance;
    return scheduler->IsEmpty() ? -1 : static_cast<int64_t>(scheduler->PeekNext().key.m_ts);
}

// Line-oriented IPC over a Unix socket inherited from the Python parent.
// IPC wall-clock time does not contribute to simulated packet delay.
class Wire
{
  public:
    explicit Wire(int fd) : m_fd(fd) {}
    std::string Read()
    {
        // Stream sockets do not preserve message boundaries; collect through newline.
        std::string line;
        char ch;
        while (true)
        {
            auto n = recv(m_fd, &ch, 1, 0);
            if (n < 0 && errno == EINTR)
            {
                continue;
            }
            Require(n == 1, n < 0 ? std::string("bridge read: ") + std::strerror(errno)
                                  : "bridge closed while reading");
            if (ch == '\n')
            {
                return line;
            }
            Require(line.size() < 4096, "bridge line exceeds 4096 bytes");
            line += ch;
        }
    }
    void Send(const std::string &line)
    {
        // Retry partial sends until the entire line has been transferred.
        std::string data = line + "\n";
        size_t offset = 0;
        while (offset < data.size())
        {
            auto n = send(m_fd, data.data() + offset, data.size() - offset, MSG_NOSIGNAL);
            if (n < 0 && errno == EINTR)
            {
                continue;
            }
            Require(n > 0, std::string("bridge write: ") + std::strerror(errno));
            offset += static_cast<size_t>(n);
        }
    }

  private:
    int m_fd;
};

template <typename... Args> void Parse(const std::string &line, Args &...args)
{
    // Check the agreed field count and order to detect protocol mismatches immediately.
    std::istringstream stream(line);
    Require(static_cast<bool>((stream >> ... >> args)), "malformed bridge line: " + line);
    std::string extra;
    Require(!(stream >> extra), "unexpected bridge fields: " + line);
}

} // namespace ns3::cosim::bridge
#endif
