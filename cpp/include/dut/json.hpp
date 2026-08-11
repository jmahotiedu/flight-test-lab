// Minimal JSON value model — only what this protocol needs.
//
// The DUT speaks line-delimited JSON, so it needs to parse one object per line
// and serialise one object back.  Pulling in a JSON library would hide exactly
// the thing this code is meant to show: a value type that owns its storage,
// with copy/move semantics that fall out of std::variant and std::vector.
#ifndef DUT_JSON_HPP
#define DUT_JSON_HPP

#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <variant>
#include <vector>

namespace dut {

class Value;

// Key/value pairs are kept in a vector rather than a map: the protocol has a
// handful of keys, insertion order is irrelevant, and serialisation sorts by
// key anyway (Python's json.dumps(..., sort_keys=True) does the same).
using Object = std::vector<std::pair<std::string, Value>>;
using Array = std::vector<Value>;

// An integer literal too large for int64_t.  Python has arbitrary-precision
// ints and echoes such a value back unchanged, so the literal text is kept and
// re-emitted verbatim rather than being clamped or turned into a float.
struct RawNumber {
  std::string text;
};

class Value {
 public:
  enum class Type { Null, Bool, Int, Double, String, Array, Object, Raw };

  Value() : storage_(std::monostate{}) {}
  Value(std::nullptr_t) : storage_(std::monostate{}) {}
  Value(bool value) : storage_(value) {}
  Value(std::int64_t value) : storage_(value) {}
  Value(int value) : storage_(static_cast<std::int64_t>(value)) {}
  Value(double value) : storage_(value) {}
  Value(std::string value) : storage_(std::move(value)) {}
  Value(const char* value) : storage_(std::string(value)) {}
  Value(Array value) : storage_(std::move(value)) {}
  Value(Object value) : storage_(std::move(value)) {}
  Value(RawNumber value) : storage_(std::move(value)) {}

  Type type() const;
  bool is_null() const { return type() == Type::Null; }
  bool is_object() const { return type() == Type::Object; }
  bool is_string() const { return type() == Type::String; }

  // Object lookup.  Returns nullptr when this is not an object, or when the
  // key is absent — the caller decides what that means (see build_response,
  // where "absent" and "explicitly null" are deliberately the same case).
  const Value* find(const std::string& key) const;

  const std::string& as_string() const;
  std::int64_t as_int() const;
  double as_double_for_dump() const;
  const Array& as_array() const;
  const Object& as_object() const;

  // Serialises with sorted keys and Python's ", " / ": " separators, so the
  // bytes on the wire match the Python DUT exactly.
  std::string dump() const;

 private:
  // Alternatives are ordered to match Type: storage_.index() is the type tag.
  std::variant<std::monostate, bool, std::int64_t, double, std::string, Array,
               Object, RawNumber>
      storage_;
};

// Parses one complete JSON document.  Returns nullopt on any syntax error or
// on trailing garbage — the DUT treats every parse failure identically
// (INVALID_JSON), so there is no error detail to propagate.
std::optional<Value> parse(const std::string& text);

}  // namespace dut

#endif  // DUT_JSON_HPP
