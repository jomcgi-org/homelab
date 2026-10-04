//! Tokenizer and chat template from a converted model directory.

use std::path::Path;

use anyhow::{Context, Result, anyhow};
use minijinja::{Environment, Error, ErrorKind, context};

pub struct Chat {
    tokenizer: tokenizers::Tokenizer,
    template: String,
}

fn raise_exception(msg: String) -> std::result::Result<String, Error> {
    Err(Error::new(ErrorKind::InvalidOperation, msg))
}

impl Chat {
    pub fn load(model_dir: &Path) -> Result<Self> {
        let tokenizer = tokenizers::Tokenizer::from_file(model_dir.join("tokenizer.json"))
            .map_err(|e| anyhow!("tokenizer.json: {e}"))?;
        let template = std::fs::read_to_string(model_dir.join("chat_template.jinja"))
            .context("chat_template.jinja")?;
        Ok(Chat {
            tokenizer,
            template,
        })
    }

    /// Renders one user turn with the model's template and a generation prompt.
    pub fn render_user(&self, user: &str) -> Result<String> {
        let mut env = Environment::new();
        minijinja_contrib::add_to_environment(&mut env);
        env.set_unknown_method_callback(minijinja_contrib::pycompat::unknown_method_callback);
        env.add_function("raise_exception", raise_exception);
        env.add_template("chat", &self.template)?;
        let out = env.get_template("chat")?.render(context! {
            messages => vec![context! { role => "user", content => user }],
            add_generation_prompt => true,
        })?;
        Ok(out)
    }

    pub fn encode(&self, text: &str) -> Result<Vec<u32>> {
        Ok(self
            .tokenizer
            .encode(text, false)
            .map_err(|e| anyhow!("encode: {e}"))?
            .get_ids()
            .to_vec())
    }

    pub fn decode(&self, ids: &[u32]) -> Result<String> {
        self.tokenizer
            .decode(ids, false)
            .map_err(|e| anyhow!("decode: {e}"))
    }

    pub fn token_id(&self, token: &str) -> Option<u32> {
        self.tokenizer.token_to_id(token)
    }
}
