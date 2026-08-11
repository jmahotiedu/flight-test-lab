#include "dut/json.hpp"

#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <sstream>
#include <stdexcept>

namespace dut {
namespace {

const std::string kEmptyString;

// Recursive-descent parser over a cursor into the input.  Every function
// returns nullopt on failure and leaves the cursor unspecified; the caller
// aborts the whole parse, which matches the protocol's all-or-nothing
// INVALID_JSON handling.
class Parser {
 public:
  explicit Parser(const std::string& text) : text_(text) {}

  std::optional<Value> parse_document() {
    skip_whitespace();
    std::optional<Value> value = parse_value(0);
    if (!value) {
      return std::nullopt;
    }
    skip_whitespace();
    if (position_ != text_.size()) {
      return std::nullopt;  // trailing garbage
    }
    return value;
  }

 private:
  static constexpr int kMaxDepth = 32;

  void skip_whitespace() {
    while (position_ < text_.size()) {
      const char c = text_[position_];
      if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
        ++position_;
      } else {
        break;
      }
    }
  }

  bool consume(char expected) {
    if (position_ < text_.size() && text_[position_] == expected) {
      ++position_;
      return true;
    }
    return false;
  }

  bool consume_literal(const char* literal) {
    const std::size_t length = std::char_traits<char>::length(literal);
    if (text_.compare(position_, length, literal) != 0) {
      return false;
    }
    position_ += length;
    return true;
  }

  std::optional<Value> parse_value(int depth) {
    if (depth > kMaxDepth) {
      return std::nullopt;  // bound the recursion; a deep input is a bad input
    }
    skip_whitespace();
    if (position_ >= text_.size()) {
      return std::nullopt;
    }
    switch (text_[position_]) {
      case '{':
        return parse_object(depth);
      case '[':
        return parse_array(depth);
      case '"': {
        std::optional<std::string> text = parse_string();
        if (!text) {
          return std::nullopt;
        }
        return Value(std::move(*text));
      }
      case 't':
        return consume_literal("true") ? std::optional<Value>(Value(true))
                                       : std::nullopt;
      case 'f':
        return consume_literal("false") ? std::optional<Value>(Value(false))
                                        : std::nullopt;
      case 'n':
        return consume_literal("null") ? std::optional<Value>(Value(nullptr))
                                       : std::nullopt;
      default:
        return parse_number();
    }
  }

  std::optional<Value> parse_object(int depth) {
    if (!consume('{')) {
      return std::nullopt;
    }
    Object object;
    skip_whitespace();
    if (consume('}')) {
      return Value(std::move(object));
    }
    while (true) {
      skip_whitespace();
      std::optional<std::string> key = parse_string();
      if (!key) {
        return std::nullopt;
      }
      skip_whitespace();
      if (!consume(':')) {
        return std::nullopt;
      }
      std::optional<Value> value = parse_value(depth + 1);
      if (!value) {
        return std::nullopt;
      }
      object.emplace_back(std::move(*key), std::move(*value));
      skip_whitespace();
      if (consume(',')) {
        continue;
      }
      if (consume('}')) {
        return Value(std::move(object));
      }
      return std::nullopt;
    }
  }

  std::optional<Value> parse_array(int depth) {
    if (!consume('[')) {
      return std::nullopt;
    }
    Array array;
    skip_whitespace();
    if (consume(']')) {
      return Value(std::move(array));
    }
    while (true) {
      std::optional<Value> value = parse_value(depth + 1);
      if (!value) {
        return std::nullopt;
      }
      array.push_back(std::move(*value));
      skip_whitespace();
      if (consume(',')) {
        continue;
      }
      if (consume(']')) {
        return Value(std::move(array));
      }
      return std::nullopt;
    }
  }

  std::optional<std::string> parse_string() {
    if (!consume('"')) {
      return std::nullopt;
    }
    std::string out;
    while (position_ < text_.size()) {
      const char c = text_[position_++];
      if (c == '"') {
        return out;
      }
      if (c != '\\') {
        out.push_back(c);
        continue;
      }
      if (position_ >= text_.size()) {
        return std::nullopt;
      }
      const char escape = text_[position_++];
      switch (escape) {
        case '"': out.push_back('"'); break;
        case '\\': out.push_back('\\'); break;
        case '/': out.push_back('/'); break;
        case 'b': out.push_back('\b'); break;
        case 'f': out.push_back('\f'); break;
        case 'n': out.push_back('\n'); break;
        case 'r': out.push_back('\r'); break;
        case 't': out.push_back('\t'); break;
        case 'u': {
          // \uXXXX: decode the code point and re-encode as UTF-8.  Surrogate
          // pairs are joined so non-BMP characters survive a round trip.
          std::optional<unsigned> code = parse_hex4();
          if (!code) {
            return std::nullopt;
          }
          unsigned code_point = *code;
          if (code_point >= 0xD800 && code_point <= 0xDBFF) {
            if (!consume('\\') || !consume('u')) {
              return std::nullopt;
            }
            std::optional<unsigned> low = parse_hex4();
            if (!low || *low < 0xDC00 || *low > 0xDFFF) {
              return std::nullopt;
            }
            code_point = 0x10000 + ((code_point - 0xD800) << 10) + (*low - 0xDC00);
          }
          append_utf8(out, code_point);
          break;
        }
        default:
          return std::nullopt;
      }
    }
    return std::nullopt;  // unterminated string
  }

  std::optional<unsigned> parse_hex4() {
    if (position_ + 4 > text_.size()) {
      return std::nullopt;
    }
    unsigned value = 0;
    for (int i = 0; i < 4; ++i) {
      const char c = text_[position_++];
      value <<= 4;
      if (c >= '0' && c <= '9') {
        value |= static_cast<unsigned>(c - '0');
      } else if (c >= 'a' && c <= 'f') {
        value |= static_cast<unsigned>(c - 'a' + 10);
      } else if (c >= 'A' && c <= 'F') {
        value |= static_cast<unsigned>(c - 'A' + 10);
      } else {
        return std::nullopt;
      }
    }
    return value;
  }

  static void append_utf8(std::string& out, unsigned code_point) {
    if (code_point < 0x80) {
      out.push_back(static_cast<char>(code_point));
    } else if (code_point < 0x800) {
      out.push_back(static_cast<char>(0xC0 | (code_point >> 6)));
      out.push_back(static_cast<char>(0x80 | (code_point & 0x3F)));
    } else if (code_point < 0x10000) {
      out.push_back(static_cast<char>(0xE0 | (code_point >> 12)));
      out.push_back(static_cast<char>(0x80 | ((code_point >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code_point & 0x3F)));
    } else {
      out.push_back(static_cast<char>(0xF0 | (code_point >> 18)));
      out.push_back(static_cast<char>(0x80 | ((code_point >> 12) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | ((code_point >> 6) & 0x3F)));
      out.push_back(static_cast<char>(0x80 | (code_point & 0x3F)));
    }
  }

  bool consume_digits() {
    const std::size_t start = position_;
    while (position_ < text_.size() && text_[position_] >= '0' &&
           text_[position_] <= '9') {
      ++position_;
    }
    return position_ > start;
  }

  // Scans the JSON number grammar exactly:
  //
  //   number = [ '-' ] ( '0' | [1-9][0-9]* ) [ '.' [0-9]+ ] [ ('e'|'E')
  //            ['+'|'-'] [0-9]+ ]
  //
  // Scanning loosely and then leaning on std::stod/std::stoll does not work:
  // both accept a valid *prefix* and ignore the rest, so "1+2" would parse as
  // 1 while Python's json rejects the whole document.  A permissive number
  // scanner is therefore a parity bug and a malformed-input bug at once.
  std::optional<Value> parse_number() {
    const std::size_t start = position_;
    if (position_ < text_.size() && text_[position_] == '-') {
      ++position_;  // a leading '+' is not legal JSON
    }

    if (position_ >= text_.size()) {
      return std::nullopt;
    }
    if (text_[position_] == '0') {
      ++position_;  // a leading zero may not be followed by more digits
      if (position_ < text_.size() && text_[position_] >= '0' &&
          text_[position_] <= '9') {
        return std::nullopt;
      }
    } else if (!consume_digits()) {
      return std::nullopt;
    }

    bool is_double = false;
    if (position_ < text_.size() && text_[position_] == '.') {
      ++position_;
      if (!consume_digits()) {
        return std::nullopt;  // "1." is not a number
      }
      is_double = true;
    }
    if (position_ < text_.size() &&
        (text_[position_] == 'e' || text_[position_] == 'E')) {
      ++position_;
      if (position_ < text_.size() &&
          (text_[position_] == '+' || text_[position_] == '-')) {
        ++position_;
      }
      if (!consume_digits()) {
        return std::nullopt;  // "1e" and "1e+" are not numbers
      }
      is_double = true;
    }

    const std::string token = text_.substr(start, position_ - start);
    if (is_double) {
      // strtod rather than stod: overflow must become ±inf and underflow ±0,
      // matching Python (json.loads("1e400") is inf, "1e-400" is 0.0), where
      // stod would throw out_of_range for both and reject a document Python
      // accepts.
      errno = 0;
      char* end = nullptr;
      const double value = std::strtod(token.c_str(), &end);
      if (end != token.c_str() + token.size()) {
        return std::nullopt;
      }
      return Value(value);
    }
    try {
      std::size_t consumed = 0;
      const long long value = std::stoll(token, &consumed);
      if (consumed != token.size()) {
        return std::nullopt;
      }
      return Value(static_cast<std::int64_t>(value));
    } catch (const std::out_of_range&) {
      // Python integers are arbitrary precision, so an integer too large for
      // int64_t is echoed back as-is instead of being lost or turned into a
      // float.  The grammar above already rejects leading zeros and '+', so
      // the token matches Python's repr of the same integer.
      return Value(RawNumber{token});
    } catch (const std::exception&) {
      return std::nullopt;
    }
  }

  const std::string& text_;
  std::size_t position_ = 0;
};

void dump_string(std::ostringstream& out, const std::string& text) {
  out << '"';
  for (const char c : text) {
    switch (c) {
      case '"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\b': out << "\\b"; break;
      case '\f': out << "\\f"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buffer[7];
          std::snprintf(buffer, sizeof(buffer), "\\u%04x",
                        static_cast<unsigned>(static_cast<unsigned char>(c)));
          out << buffer;
        } else {
          out << c;
        }
    }
  }
  out << '"';
}

// Reproduces Python's float repr, which is what json.dumps emits: the shortest
// decimal string that round-trips, with a ".0" forced onto integral values so
// 1.5e3 prints as 1500.0 and not 1500.  std::to_chars gives the same shortest
// round-trip digits; only the trailing ".0" has to be added by hand.
// Non-finite values follow Python's json, which emits the JavaScript spellings
// rather than failing.
std::string format_double(double value) {
  if (std::isnan(value)) {
    return "NaN";
  }
  if (std::isinf(value)) {
    return value > 0 ? "Infinity" : "-Infinity";
  }
  char buffer[64];
  const std::to_chars_result result =
      std::to_chars(buffer, buffer + sizeof(buffer), value);
  std::string text(buffer, result.ptr);
  if (text.find('.') == std::string::npos &&
      text.find('e') == std::string::npos &&
      text.find('E') == std::string::npos) {
    text += ".0";
  }
  return text;
}

void dump_value(std::ostringstream& out, const Value& value);

void dump_object(std::ostringstream& out, const Object& object) {
  // Sorted keys and ", " / ": " separators reproduce Python's
  // json.dumps(payload, sort_keys=True) byte for byte, so a captured response
  // from either DUT compares equal.
  std::vector<const std::pair<std::string, Value>*> entries;
  entries.reserve(object.size());
  for (const auto& entry : object) {
    entries.push_back(&entry);
  }
  std::stable_sort(entries.begin(), entries.end(),
                   [](const auto* left, const auto* right) {
                     return left->first < right->first;
                   });
  out << '{';
  bool first = true;
  for (const auto* entry : entries) {
    if (!first) {
      out << ", ";
    }
    first = false;
    dump_string(out, entry->first);
    out << ": ";
    dump_value(out, entry->second);
  }
  out << '}';
}

void dump_value(std::ostringstream& out, const Value& value) {
  switch (value.type()) {
    case Value::Type::Null:
      out << "null";
      break;
    case Value::Type::Bool:
      out << (value.as_int() != 0 ? "true" : "false");
      break;
    case Value::Type::Int:
      out << value.as_int();
      break;
    case Value::Type::Double:
      out << format_double(value.as_double_for_dump());
      break;
    case Value::Type::Raw:
      out << value.as_string();  // echoed exactly as it arrived
      break;
    case Value::Type::String:
      dump_string(out, value.as_string());
      break;
    case Value::Type::Array: {
      out << '[';
      bool first = true;
      for (const Value& item : value.as_array()) {
        if (!first) {
          out << ", ";
        }
        first = false;
        dump_value(out, item);
      }
      out << ']';
      break;
    }
    case Value::Type::Object:
      dump_object(out, value.as_object());
      break;
  }
}

}  // namespace

Value::Type Value::type() const {
  return static_cast<Type>(storage_.index());
}

const Value* Value::find(const std::string& key) const {
  const Object* object = std::get_if<Object>(&storage_);
  if (object == nullptr) {
    return nullptr;
  }
  for (const auto& entry : *object) {
    if (entry.first == key) {
      return &entry.second;
    }
  }
  return nullptr;
}

const std::string& Value::as_string() const {
  if (const std::string* text = std::get_if<std::string>(&storage_)) {
    return *text;
  }
  if (const RawNumber* raw = std::get_if<RawNumber>(&storage_)) {
    return raw->text;  // lets the serialiser echo the literal unchanged
  }
  return kEmptyString;
}

std::int64_t Value::as_int() const {
  if (const std::int64_t* number = std::get_if<std::int64_t>(&storage_)) {
    return *number;
  }
  if (const bool* flag = std::get_if<bool>(&storage_)) {
    return *flag ? 1 : 0;
  }
  if (const double* number = std::get_if<double>(&storage_)) {
    return static_cast<std::int64_t>(*number);
  }
  return 0;
}

double Value::as_double_for_dump() const {
  const double* number = std::get_if<double>(&storage_);
  return number != nullptr ? *number : 0.0;
}

const Array& Value::as_array() const {
  static const Array kEmpty;
  const Array* array = std::get_if<Array>(&storage_);
  return array != nullptr ? *array : kEmpty;
}

const Object& Value::as_object() const {
  static const Object kEmpty;
  const Object* object = std::get_if<Object>(&storage_);
  return object != nullptr ? *object : kEmpty;
}

std::string Value::dump() const {
  std::ostringstream out;
  dump_value(out, *this);
  return out.str();
}

std::optional<Value> parse(const std::string& text) {
  Parser parser(text);
  return parser.parse_document();
}

}  // namespace dut
