package com.acme.user;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import java.util.List;

/** 사용자 비즈니스 로직 */
@Service
public class UserService {

    @Value("${user.cache.ttl-seconds:600}")
    private int cacheTtl;

    private final UserRepository repo;

    public UserService(UserRepository repo) { this.repo = repo; }

    public UserDto find(Long id) {
        return repo.findById(id).map(u -> new UserDto(u.getName())).orElseThrow();
    }

    public List<UserDto> list(String status) { return List.of(); }

    public void register(UserDto dto) { }

    public UserDto update(Long id, UserDto dto) { return dto; }

    private void audit() { }
}
