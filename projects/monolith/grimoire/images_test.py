from grimoire import images


def test_parse_s3_uri_requires_bucket_and_key():
    assert images.parse_s3_uri("s3://bucket/dir/a.png") == ("bucket", "dir/a.png")
    assert images.parse_s3_uri("s3://bucket") is None
    assert images.parse_s3_uri("s3:///key") is None
    assert images.parse_s3_uri("http://bucket/key") is None


def test_content_type_from_suffix():
    assert images.content_type("a/b.JPG") == "image/jpeg"
    assert images.content_type("a/b.webp") == "image/webp"
    assert images.content_type("a/b.bin") == "application/octet-stream"
    assert images.content_type("nosuffix") == "application/octet-stream"


def test_iter_body_closes_after_draining():
    class Body:
        closed = False

        def iter_chunks(self, chunk_size):
            yield b"ab"
            yield b"c"

        def close(self):
            self.closed = True

    body = Body()
    assert b"".join(images.iter_body(body)) == b"abc"
    assert body.closed
