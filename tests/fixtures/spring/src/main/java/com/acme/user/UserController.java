package com.acme.user;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;
import java.util.List;

/**
 * 사용자 관리 API.
 * 등록/조회/삭제를 제공한다.
 */
@RestController
@RequestMapping("/api/users")
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    /** 사용자 단건 조회 */
    @GetMapping("/{id}")
    public ResponseEntity<UserDto> get(@PathVariable("id") Long id) {
        return ResponseEntity.ok(userService.find(id));
    }

    /** 사용자 목록 조회 */
    @GetMapping
    public List<UserDto> list(@RequestParam(defaultValue = "ACTIVE") String status, HttpServletRequest req) {
        return userService.list(status);
    }

    @PostMapping(value = "/register")
    public ResponseEntity<Void> register(@RequestBody UserDto dto) {
        userService.register(dto);
        return ResponseEntity.ok().build();
    }

    @RequestMapping(value = "/{id}", method = {RequestMethod.PUT, RequestMethod.PATCH})
    public UserDto update(@PathVariable Long id, @RequestBody UserDto dto) {
        return userService.update(id, dto);
    }
}
