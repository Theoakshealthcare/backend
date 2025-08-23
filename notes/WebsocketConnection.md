// Replace with your backend's websocket URL and the user's JWT token
const token = "<JWT_TOKEN>";
const wsUrl = `wss://your-backend-domain/ws/${token}`;
const socket = new WebSocket(wsUrl);

socket.onopen = () => {
  console.log("WebSocket connected");
  // Optionally, join a booking room
  // socket.send(JSON.stringify({ type: "join_booking", booking_id: "<booking-uuid>" }));
};

socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  switch (message.type) {
    case "new_booking_request":
      // Handle new booking notification (for caregivers)
      console.log("New booking request:", message.data);
      // Show notification in UI, play sound, etc.
      break;
    case "booking_update":
      // Handle booking status updates
      console.log("Booking update:", message.data);
      break;
    case "chat_message":
      // Handle incoming chat message
      console.log("Chat message:", message);
      break;
    case "caregiver_location":
      // Handle real-time caregiver location update
      console.log("Caregiver location update:", message);
      break;
    default:
      console.log("Unknown message type:", message);
  }
};

socket.onclose = () => {
  console.log("WebSocket disconnected");
};

socket.onerror = (err) => {
  console.error("WebSocket error:", err);
};