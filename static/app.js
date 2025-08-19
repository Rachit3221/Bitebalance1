// Minimal JS for Socket.IO group chat + UI niceties
document.addEventListener("DOMContentLoaded", () => {
  const flash = document.querySelector(".flash");
  if (flash) setTimeout(()=> flash.remove(), 3000);

  // Group chat page hookup
  const roomEl = document.getElementById("room_name");
  const msgForm = document.getElementById("msg_form");
  const msgInput = document.getElementById("msg_text");
  const chatBox = document.getElementById("chat_box");
  
  if (roomEl && msgForm && msgInput && chatBox) {
    const socket = io();
    const room = roomEl.value;
    socket.emit("join", { room });

    msgForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const text = msgInput.value.trim();
      if (!text) return;
      socket.emit("message", { room, text });
      msgInput.value = "";
      msgInput.focus();
    });

    socket.on("message", (data) => {
      const div = document.createElement("div");
      div.className = "msg fade-in";
      div.innerHTML = `<span class="who">${data.username}</span> ${data.text} <span class="at">${data.created_at}</span>`;
      chatBox.appendChild(div);
      chatBox.scrollTop = chatBox.scrollHeight;
    });
    
    // Ensure chat box is scrolled to bottom on page load
    chatBox.scrollTop = chatBox.scrollHeight;
  }
});