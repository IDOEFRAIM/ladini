"""Script de test à blanc pour valider le format du Chatbot Gradio."""

import gradio as gr


def respond_test(message, chat_history):
    if not message.strip():
        return "", chat_history

    if chat_history is None:
        chat_history = []

    # Message de test simple sans IA
    bot_message = f"👋 Salut ! Connexion Gradio réussie. Tu as écrit : '{message}'"

    # Structure attendue par ton validateur Gradio
    chat_history.append({"role": "user", "content": str(message)})
    chat_history.append({"role": "assistant", "content": str(bot_message)})

    return "", chat_history


with gr.Blocks() as demo:
    gr.Markdown("# 🧪 Test d'isolation Gradio")

    chatbot = gr.Chatbot(label="Console WhatsApp (Test)", height=400)
    msg_input = gr.Textbox(
        label="Écris quelque chose pour tester le format", placeholder="Ex: Hello..."
    )

    msg_input.submit(respond_test, [msg_input, chatbot], [msg_input, chatbot])

if __name__ == "__main__":
    demo.launch(share=False)