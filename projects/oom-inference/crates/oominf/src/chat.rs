//! Chat-turn helpers for the CLI tools, on top of the server's [`ChatTemplate`].

use std::path::Path;

use anyhow::Result;
use oominf_server::template::{ChatTemplate, TemplateOptions};
use serde_json::json;

pub struct Chat(ChatTemplate);

impl Chat {
    pub fn load(model_dir: &Path) -> Result<Self> {
        Ok(Chat(ChatTemplate::load(model_dir)?))
    }

    /// Renders one user turn with the model's template and a generation prompt.
    pub fn render_user(&self, user: &str) -> Result<String> {
        self.render_user_options(user, &TemplateOptions::default())
    }

    pub fn render_user_options(&self, user: &str, options: &TemplateOptions) -> Result<String> {
        self.0
            .render(&[json!({ "role": "user", "content": user })], None, options)
    }

    pub fn encode(&self, text: &str) -> Result<Vec<u32>> {
        self.0.encode(text)
    }

    pub fn decode(&self, ids: &[u32]) -> Result<String> {
        self.0.decode(ids)
    }

    pub fn token_id(&self, token: &str) -> Option<u32> {
        self.0.token_id(token)
    }
}
