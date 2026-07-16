function AIAgents() {
  const agents = [
    {
      name: "Clip Finder",
      status: "Running",
      description: "Detects viral Twitch moments automatically.",
    },
    {
      name: "Title Generator",
      status: "Ready",
      description: "Creates catchy clip titles using AI.",
    },
    {
      name: "Publishing Agent",
      status: "Running",
      description: "Schedules content for social platforms.",
    },
  ];

  return (
    <div>
      <h1>AI Agents</h1>
      <p>Your autonomous AI workforce.</p>

      {agents.map((agent, index) => (
        <div
          key={index}
          style={{
            marginTop: "20px",
            padding: "20px",
            border: "1px solid #3d4d7a",
            borderRadius: "12px",
            background: "#1a2342",
          }}
        >
          <h2>{agent.name}</h2>
          <p>Status: {agent.status}</p>
          <p>{agent.description}</p>
        </div>
      ))}
    </div>
  );
}

export default AIAgents;