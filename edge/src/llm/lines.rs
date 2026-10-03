//! A streamed response body as lines — what NDJSON (Ollama) and SSE (Gemini,
//! and the OpenAI and Anthropic formats to come) are both made of.

use futures_util::StreamExt;

use super::Error;

pub struct Lines {
    body: futures_util::stream::BoxStream<'static, reqwest::Result<bytes::Bytes>>,
    buf: Vec<u8>,
    done: bool,
}

impl Lines {
    pub fn new(resp: reqwest::Response) -> Self {
        Lines { body: resp.bytes_stream().boxed(), buf: vec![], done: false }
    }

    /// The next line without its `\n` / `\r\n`, or `None` at the end. A last
    /// line with no newline after it still counts.
    pub async fn next(&mut self) -> Result<Option<String>, Error> {
        loop {
            if let Some(i) = self.buf.iter().position(|&b| b == b'\n') {
                let mut line: Vec<u8> = self.buf.drain(..=i).collect();
                line.pop();
                if line.last() == Some(&b'\r') {
                    line.pop();
                }
                return Ok(Some(String::from_utf8_lossy(&line).into_owned()));
            }
            if self.done {
                if self.buf.is_empty() {
                    return Ok(None);
                }
                let line = String::from_utf8_lossy(&std::mem::take(&mut self.buf)).into_owned();
                return Ok(Some(line));
            }
            match self.body.next().await {
                Some(Ok(chunk)) => self.buf.extend_from_slice(&chunk),
                Some(Err(e)) => return Err(Error::connection(e)),
                None => self.done = true,
            }
        }
    }

    /// The next server-sent event's data (its `data:` lines joined), or
    /// `None` at the end. Comments and other fields are skipped.
    pub async fn next_event(&mut self) -> Result<Option<String>, Error> {
        let mut data: Option<String> = None;
        while let Some(line) = self.next().await? {
            if line.is_empty() {
                if data.is_some() {
                    return Ok(data);
                }
                continue;
            }
            if let Some(rest) = line.strip_prefix("data:") {
                let rest = rest.strip_prefix(' ').unwrap_or(rest);
                match &mut data {
                    Some(d) => {
                        d.push('\n');
                        d.push_str(rest);
                    }
                    None => data = Some(rest.to_string()),
                }
            }
        }
        Ok(data)
    }
}
