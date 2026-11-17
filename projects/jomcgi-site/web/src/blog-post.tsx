import { Shell } from "./Shell";
import { BlogEmbed } from "./blog-embed";
import { Tags, formatDate } from "./blog-shared";
import type { Post } from "./blog-types";
import "./blog.css";

export function BlogPost({ post }: { post: Post }) {
  return (
    <Shell>
      <article className="blog">
        <nav aria-label="Blog navigation">
          <a href="/blog">blog</a>
        </nav>
        <header>
          <time dateTime={post.date}>{formatDate(post.date)}</time>
          <h1>{post.title}</h1>
          <p>{post.summary}</p>
          <Tags tags={post.tags} />
        </header>
        {post.slug === "125b-on-a-4090" && (
          <section aria-labelledby="inference-demo">
            <h2 id="inference-demo">Inference Demo</h2>
            <p>
              No further weight quantisation: the NVFP4 release on one 24 GB
              card
            </p>
            <table>
              <caption>Recorded on the RTX 4090</caption>
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Input tokens</th>
                  <th scope="col">First token</th>
                  <th scope="col">Decode</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <th scope="row">Read an incident report</th>
                  <td>21,878</td>
                  <td>6.95 s</td>
                  <td>45.4 tok/s</td>
                </tr>
                <tr>
                  <th scope="row">Rewrite a source file</th>
                  <td>23,723</td>
                  <td>7.33 s</td>
                  <td>46.3 tok/s</td>
                </tr>
              </tbody>
            </table>
            <BlogEmbed kind="research" label="Long-context research" />
            <BlogEmbed kind="coding" label="Agentic coding" />
            <a href="#post-body">Read the post ↓</a>
          </section>
        )}
        {!!post.toc.length && (
          <nav aria-label="Sections">
            <ol>
              {post.toc.map((section) => (
                <li
                  key={section.id}
                  className={
                    section.depth === 3 ? "blog-subsection" : undefined
                  }
                >
                  <a href={`#${section.id}`}>{section.text}</a>
                </li>
              ))}
            </ol>
          </nav>
        )}
        <div
          id="post-body"
          className="blog-prose"
          dangerouslySetInnerHTML={{
            __html:
              post.slug === "125b-on-a-4090"
                ? post.html.replace(/<h2 id="inference-demo">.*?<\/h2>\n/, "")
                : post.html,
          }}
        />
        {post.slug === "ember-conformance" && (
          <BlogEmbed kind="conformance" label="Conformance replay" />
        )}
      </article>
    </Shell>
  );
}
